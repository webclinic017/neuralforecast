# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/models_nhits__nhits.ipynb (unless otherwise specified).

__all__ = ['ACTIVATIONS', 'NHITS', 'suggested_space']

# Cell
import math
import random
from functools import partial
from typing import Tuple, List
from fastcore.foundation import patch

import numpy as np
import pandas as pd
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
import pytorch_lightning as pl
from hyperopt import hp

from ..components.tcn import _TemporalConvNet
from ..components.common import Chomp1d, RepeatVector
from ...losses.utils import LossFunction
from ...data.tsdataset import WindowsDataset
from ...data.tsloader import TimeSeriesLoader

# Cell
class _StaticFeaturesEncoder(nn.Module):
    def __init__(self, in_features, out_features):
        super(_StaticFeaturesEncoder, self).__init__()
        layers = [nn.Dropout(p=0.5),
                  nn.Linear(in_features=in_features, out_features=out_features),
                  nn.ReLU()]
        self.encoder = nn.Sequential(*layers)

    def forward(self, x):
        x = self.encoder(x)
        return x

class _sEncoder(nn.Module):
    def __init__(self, in_features, out_features, n_time_in):
        super(_sEncoder, self).__init__()
        layers = [nn.Dropout(p=0.5),
                  nn.Linear(in_features=in_features, out_features=out_features),
                  nn.ReLU()]
        self.encoder = nn.Sequential(*layers)
        self.repeat = RepeatVector(repeats=n_time_in)

    def forward(self, x):
        # Encode and repeat values to match time
        x = self.encoder(x)
        x = self.repeat(x) # [N,S_out] -> [N,S_out,T]
        return x

# Cell
class _IdentityBasis(nn.Module):
    def __init__(self, backcast_size: int, forecast_size: int, interpolation_mode: str):
        super().__init__()
        assert (interpolation_mode in ['linear','nearest']) or ('cubic' in interpolation_mode)
        self.forecast_size = forecast_size
        self.backcast_size = backcast_size
        self.interpolation_mode = interpolation_mode

    def forward(self, theta: t.Tensor, insample_x_t: t.Tensor, outsample_x_t: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:

        backcast = theta[:, :self.backcast_size]
        knots = theta[:, self.backcast_size:]

        if self.interpolation_mode=='nearest':
            knots = knots[:,None,:]
            forecast = F.interpolate(knots, size=self.forecast_size, mode=self.interpolation_mode)
            forecast = forecast[:,0,:]
        elif self.interpolation_mode=='linear':
            knots = knots[:,None,:]
            forecast = F.interpolate(knots, size=self.forecast_size, mode=self.interpolation_mode)
            forecast = forecast[:,0,:]
        elif 'cubic' in self.interpolation_mode:
            batch_size = len(backcast)
            knots = knots[:,None,None,:]
            forecast = t.zeros((len(knots), self.forecast_size)).to(knots.device)
            n_batches = int(np.ceil(len(knots)/batch_size))
            for i in range(n_batches):
                forecast_i = F.interpolate(knots[i*batch_size:(i+1)*batch_size], size=self.forecast_size, mode='bicubic')
                forecast[i*batch_size:(i+1)*batch_size] += forecast_i[:,0,0,:]

        return backcast, forecast

# Cell
class _ExogenousBasisInterpretable(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, theta: t.Tensor, insample_x_t: t.Tensor, outsample_x_t: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:
        backcast_basis = insample_x_t
        forecast_basis = outsample_x_t

        cut_point = forecast_basis.shape[1]
        backcast = t.einsum('bp,bpt->bt', theta[:, cut_point:], backcast_basis)
        forecast = t.einsum('bp,bpt->bt', theta[:, :cut_point], forecast_basis)
        return backcast, forecast

class _ExogenousBasisWavenet(nn.Module):
    def __init__(self, out_features, in_features, num_levels=4, kernel_size=3, dropout_prob=0):
        super().__init__()
        # Shape of (1, in_features, 1) to broadcast over b and t
        self.weight = nn.Parameter(t.Tensor(1, in_features, 1), requires_grad=True)
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(0.5))

        padding = (kernel_size - 1) * (2**0)
        input_layer = [nn.Conv1d(in_channels=in_features, out_channels=out_features,
                                 kernel_size=kernel_size, padding=padding, dilation=2**0),
                                 Chomp1d(padding),
                                 nn.ReLU(),
                                 nn.Dropout(dropout_prob)]
        conv_layers = []
        for i in range(1, num_levels):
            dilation = 2**i
            padding = (kernel_size - 1) * dilation
            conv_layers.append(nn.Conv1d(in_channels=out_features, out_channels=out_features,
                                         padding=padding, kernel_size=3, dilation=dilation))
            conv_layers.append(Chomp1d(padding))
            conv_layers.append(nn.ReLU())
        conv_layers = input_layer + conv_layers

        self.wavenet = nn.Sequential(*conv_layers)

    def transform(self, insample_x_t, outsample_x_t):
        n_time_in = insample_x_t.shape[2]

        x_t = t.cat([insample_x_t, outsample_x_t], dim=2)

        x_t = x_t * self.weight # Element-wise multiplication, broadcasted on b and t. Weights used in L1 regularization
        x_t = self.wavenet(x_t)[:]

        backcast_basis = x_t[:,:, :n_time_in]
        forecast_basis = x_t[:,:, n_time_in:]

        return backcast_basis, forecast_basis

    def forward(self, theta: t.Tensor, insample_x_t: t.Tensor, outsample_x_t: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:
        backcast_basis, forecast_basis = self.transform(insample_x_t, outsample_x_t)

        cut_point = forecast_basis.shape[1]
        backcast = t.einsum('bp,bpt->bt', theta[:, cut_point:], backcast_basis)
        forecast = t.einsum('bp,bpt->bt', theta[:, :cut_point], forecast_basis)
        return backcast, forecast

class _ExogenousBasisTCN(nn.Module):
    def __init__(self, out_features, in_features, num_levels = 4, kernel_size=2, dropout_prob=0):
        super().__init__()
        n_channels = num_levels * [out_features]
        self.tcn = _TemporalConvNet(num_inputs=in_features, num_channels=n_channels, kernel_size=kernel_size, dropout=dropout_prob)

    def transform(self, insample_x_t, outsample_x_t):
        n_time_in = insample_x_t.shape[2]

        x_t = t.cat([insample_x_t, outsample_x_t], dim=2)

        x_t = self.tcn(x_t)[:]
        backcast_basis = x_t[:,:, :n_time_in]
        forecast_basis = x_t[:,:, n_time_in:]

        return backcast_basis, forecast_basis

    def forward(self, theta: t.Tensor, insample_x_t: t.Tensor, outsample_x_t: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:
        backcast_basis, forecast_basis = self.transform(insample_x_t, outsample_x_t)

        cut_point = forecast_basis.shape[1]
        backcast = t.einsum('bp,bpt->bt', theta[:, cut_point:], backcast_basis)
        forecast = t.einsum('bp,bpt->bt', theta[:, :cut_point], forecast_basis)
        return backcast, forecast

# Cell
def _init_weights(module, initialization):
    if type(module) == t.nn.Linear:
        if initialization == 'orthogonal':
            t.nn.init.orthogonal_(module.weight)
        elif initialization == 'he_uniform':
            t.nn.init.kaiming_uniform_(module.weight)
        elif initialization == 'he_normal':
            t.nn.init.kaiming_normal_(module.weight)
        elif initialization == 'glorot_uniform':
            t.nn.init.xavier_uniform_(module.weight)
        elif initialization == 'glorot_normal':
            t.nn.init.xavier_normal_(module.weight)
        elif initialization == 'lecun_normal':
            pass #t.nn.init.normal_(module.weight, 0.0, std=1/np.sqrt(module.weight.numel()))
        else:
            assert 1<0, f'Initialization {initialization} not found'

# Cell
ACTIVATIONS = ['ReLU',
               'Softplus',
               'Tanh',
               'SELU',
               'LeakyReLU',
               'PReLU',
               'Sigmoid']

class _NHITSBlock(nn.Module):
    """
    N-BEATS block which takes a basis function as an argument.
    """
    def __init__(self, n_time_in: int, n_time_out: int, n_x: int,
                 n_s: int, n_s_hidden: int, n_theta: int, n_mlp_units: list,
                 n_pool_kernel_size: int, pooling_mode: str, basis: nn.Module,
                 n_layers: int,  batch_normalization: bool, dropout_prob: float, activation: str):
        """
        """
        super().__init__()

        assert (pooling_mode in ['max','average'])

        n_time_in_pooled = int(np.ceil(n_time_in/n_pool_kernel_size))

        if n_s == 0:
            n_s_hidden = 0
        n_mlp_units = [n_time_in_pooled + (n_time_in+n_time_out)*n_x + n_s_hidden] + n_mlp_units

        self.n_time_in = n_time_in
        self.n_time_out = n_time_out
        self.n_s = n_s
        self.n_s_hidden = n_s_hidden
        self.n_x = n_x
        self.n_pool_kernel_size = n_pool_kernel_size
        self.batch_normalization = batch_normalization
        self.dropout_prob = dropout_prob

        assert activation in ACTIVATIONS, f'{activation} is not in {ACTIVATIONS}'
        activ = getattr(nn, activation)()

        if pooling_mode == 'max':
            self.pooling_layer = nn.MaxPool1d(kernel_size=self.n_pool_kernel_size,
                                              stride=self.n_pool_kernel_size, ceil_mode=True)
        elif pooling_mode == 'average':
            self.pooling_layer = nn.AvgPool1d(kernel_size=self.n_pool_kernel_size,
                                              stride=self.n_pool_kernel_size, ceil_mode=True)

        hidden_layers = []
        for i in range(n_layers):
            hidden_layers.append(nn.Linear(in_features=n_mlp_units[i], out_features=n_mlp_units[i+1]))
            hidden_layers.append(activ)

            if self.batch_normalization:
                hidden_layers.append(nn.BatchNorm1d(num_features=n_mlp_units[i+1]))

            if self.dropout_prob>0:
                hidden_layers.append(nn.Dropout(p=self.dropout_prob))

        output_layer = [nn.Linear(in_features=n_mlp_units[-1], out_features=n_theta)]
        layers = hidden_layers + output_layer

        # n_s is computed with data, n_s_hidden is provided by user, if 0 no statics are used
        if (self.n_s > 0) and (self.n_s_hidden > 0):
            self.static_encoder = _StaticFeaturesEncoder(in_features=n_s, out_features=n_s_hidden)
        self.layers = nn.Sequential(*layers)
        self.basis = basis

    def forward(self, insample_y: t.Tensor, insample_x_t: t.Tensor,
                outsample_x_t: t.Tensor, x_s: t.Tensor) -> Tuple[t.Tensor, t.Tensor]:

        insample_y = insample_y.unsqueeze(1)
        insample_y = self.pooling_layer(insample_y)
        insample_y = insample_y.squeeze(1)

        batch_size = len(insample_y)
        if self.n_x > 0:
            insample_y = t.cat(( insample_y, insample_x_t.reshape(batch_size, -1) ), 1)
            insample_y = t.cat(( insample_y, outsample_x_t.reshape(batch_size, -1) ), 1)

        # Static exogenous
        if (self.n_s > 0) and (self.n_s_hidden > 0):
            x_s = self.static_encoder(x_s)
            insample_y = t.cat((insample_y, x_s), 1)

        # Compute local projection weights and projection
        theta = self.layers(insample_y)
        backcast, forecast = self.basis(theta, insample_x_t, outsample_x_t)

        return backcast, forecast

# Cell
class _NHITS(nn.Module):
    """
    NHITS Model.
    """
    def __init__(self,
                 n_time_in,
                 n_time_out,
                 n_s,
                 n_x,
                 n_s_hidden,
                 n_x_hidden,
                 stack_types: list,
                 n_blocks: list,
                 n_layers: list,
                 n_mlp_units: list,
                 n_pool_kernel_size: list,
                 n_freq_downsample: list,
                 pooling_mode,
                 interpolation_mode,
                 dropout_prob_theta,
                 activation,
                 initialization,
                 batch_normalization,
                 shared_weights):
        super().__init__()

        self.n_time_out = n_time_out

        blocks = self.create_stack(stack_types=stack_types,
                                   n_blocks=n_blocks,
                                   n_time_in=n_time_in,
                                   n_time_out=n_time_out,
                                   n_x=n_x,
                                   n_x_hidden=n_x_hidden,
                                   n_s=n_s,
                                   n_s_hidden=n_s_hidden,
                                   n_layers=n_layers,
                                   n_mlp_units=n_mlp_units,
                                   n_pool_kernel_size=n_pool_kernel_size,
                                   n_freq_downsample=n_freq_downsample,
                                   pooling_mode=pooling_mode,
                                   interpolation_mode=interpolation_mode,
                                   batch_normalization=batch_normalization,
                                   dropout_prob_theta=dropout_prob_theta,
                                   activation=activation,
                                   shared_weights=shared_weights,
                                   initialization=initialization)
        self.blocks = t.nn.ModuleList(blocks)

    def create_stack(self, stack_types, n_blocks,
                     n_time_in, n_time_out,
                     n_x, n_x_hidden, n_s, n_s_hidden,
                     n_layers, n_mlp_units,
                     n_pool_kernel_size, n_freq_downsample, pooling_mode, interpolation_mode,
                     batch_normalization, dropout_prob_theta,
                     activation, shared_weights, initialization):
        block_list = []
        for i in range(len(stack_types)):
            assert stack_types[i] in ['identity', 'exogenous', 'exogenous_tcn', 'exogenous_wavenet'], 'f Invalid stack type {stack_types[i]}'
            for block_id in range(n_blocks[i]):

                # Batch norm only on first block
                if (len(block_list)==0) and (batch_normalization):
                    batch_normalization_block = True
                else:
                    batch_normalization_block = False

                # Shared weights
                if shared_weights and block_id>0:
                    nbeats_block = block_list[-1]
                else:
                    if stack_types[i] == 'identity':
                        n_theta = (n_time_in + max(n_time_out//n_freq_downsample[i], 1) )
                        basis = _IdentityBasis(backcast_size=n_time_in,
                                              forecast_size=n_time_out,
                                              interpolation_mode=interpolation_mode)

                    elif stack_types[i] == 'exogenous':
                        n_theta = 2 * n_x
                        basis = _ExogenousBasisInterpretable()

                    elif stack_types[i] == 'exogenous_tcn':
                        n_theta = 2 * n_x_hidden
                        basis = _ExogenousBasisTCN(n_x_hidden, n_x)

                    elif stack_types[i] == 'exogenous_wavenet':
                        n_theta = 2 * n_x_hidden
                        basis = _ExogenousBasisWavenet(n_x_hidden, n_x)

                    nbeats_block = _NHITSBlock(n_time_in=n_time_in,
                                               n_time_out=n_time_out,
                                               n_x=n_x,
                                               n_s=n_s,
                                               n_s_hidden=n_s_hidden,
                                               n_theta=n_theta,
                                               n_mlp_units=n_mlp_units[i],
                                               n_pool_kernel_size=n_pool_kernel_size[i],
                                               pooling_mode=pooling_mode,
                                               basis=basis,
                                               n_layers=n_layers[i],
                                               batch_normalization=batch_normalization_block,
                                               dropout_prob=dropout_prob_theta,
                                               activation=activation)

                # Select type of evaluation and apply it to all layers of block
                init_function = partial(_init_weights, initialization=initialization)
                nbeats_block.layers.apply(init_function)
                block_list.append(nbeats_block)
        return block_list

    def forward(self, S: t.Tensor, Y: t.Tensor, X: t.Tensor,
                insample_mask: t.Tensor, outsample_mask: t.Tensor,
                return_decomposition: bool=False):

        # insample
        insample_y    = Y[:, :-self.n_time_out]
        insample_x_t  = X[:, :, :-self.n_time_out]
        insample_mask = insample_mask[:, :-self.n_time_out]

        # outsample
        outsample_y   = Y[:, -self.n_time_out:]
        outsample_x_t = X[:, :, -self.n_time_out:]
        outsample_mask = outsample_mask[:, -self.n_time_out:]

        if return_decomposition:
            forecast, block_forecasts = self.forecast_decomposition(insample_y=insample_y,
                                                                    insample_x_t=insample_x_t,
                                                                    insample_mask=insample_mask,
                                                                    outsample_x_t=outsample_x_t,
                                                                    x_s=S)
            return outsample_y, forecast, block_forecasts, outsample_mask

        else:
            forecast = self.forecast(insample_y=insample_y,
                                     insample_x_t=insample_x_t,
                                     insample_mask=insample_mask,
                                     outsample_x_t=outsample_x_t,
                                     x_s=S)
            return outsample_y, forecast, outsample_mask

    def forecast(self, insample_y: t.Tensor, insample_x_t: t.Tensor, insample_mask: t.Tensor,
                 outsample_x_t: t.Tensor, x_s: t.Tensor):

        residuals = insample_y.flip(dims=(-1,))
        insample_x_t = insample_x_t.flip(dims=(-1,))
        insample_mask = insample_mask.flip(dims=(-1,))

        forecast = insample_y[:, -1:] # Level with Naive1
        for i, block in enumerate(self.blocks):
            backcast, block_forecast = block(insample_y=residuals, insample_x_t=insample_x_t,
                                             outsample_x_t=outsample_x_t, x_s=x_s)
            residuals = (residuals - backcast) * insample_mask
            forecast = forecast + block_forecast

        return forecast

    def forecast_decomposition(self, insample_y: t.Tensor, insample_x_t: t.Tensor, insample_mask: t.Tensor,
                               outsample_x_t: t.Tensor, x_s: t.Tensor):

        residuals = insample_y.flip(dims=(-1,))
        insample_x_t = insample_x_t.flip(dims=(-1,))
        insample_mask = insample_mask.flip(dims=(-1,))

        n_batch, n_channels, n_t = outsample_x_t.size(0), outsample_x_t.size(1), outsample_x_t.size(2)

        level = insample_y[:, -1:] # Level with Naive1
        block_forecasts = [ level.repeat(1, n_t) ]

        forecast = level
        for i, block in enumerate(self.blocks):
            backcast, block_forecast = block(insample_y=residuals, insample_x_t=insample_x_t,
                                             outsample_x_t=outsample_x_t, x_s=x_s)
            residuals = (residuals - backcast) * insample_mask
            forecast = forecast + block_forecast
            block_forecasts.append(block_forecast)

        # (n_batch, n_blocks, n_t)
        block_forecasts = t.stack(block_forecasts)
        block_forecasts = block_forecasts.permute(1,0,2)

        return forecast, block_forecasts

# Cell
class NHITS(pl.LightningModule):
    def __init__(self,
                 n_time_in: int,
                 n_time_out: int,
                 n_x: int,
                 n_s: int,
                 shared_weights: bool,
                 activation: str,
                 initialization: str,
                 stack_types: List[str],
                 n_blocks: List[int],
                 n_layers: List[int],
                 n_mlp_units: List[List[int]],
                 n_x_hidden: int,
                 n_s_hidden: int,
                 n_pool_kernel_size: List[int],
                 n_freq_downsample: List[int],
                 pooling_mode: str,
                 interpolation_mode: str,
                 batch_normalization: bool,
                 dropout_prob_theta: float,
                 learning_rate: float,
                 lr_decay: float,
                 lr_decay_step_size: int,
                 weight_decay: float,
                 loss_train: str,
                 loss_hypar: float,
                 loss_valid: str,
                 frequency: str,
                 random_seed: int):
        """
        N-HiTS model.

            Parameters
            ----------
            n_time_in: int
                Multiplier to get insample size.
                Insample size = n_time_in * output_size
            n_time_out: int
                Forecast horizon.
            n_x: int
                Number of exogenous variables.
            n_s: int
                Number of static variables.
            shared_weights: bool
                If True, all blocks within each stack will share parameters.
            activation: str
                Activation function.
                An item from ['ReLU', 'Softplus', 'Tanh', 'SELU', 'LeakyReLU', 'PReLU', 'Sigmoid'].
            initialization: str
                Initialization function.
                An item from ['orthogonal', 'he_uniform', 'glorot_uniform', 'glorot_normal', 'lecun_normal'].
            stack_types: List[str]
                List of stack types.
                Subset from ['seasonality', 'trend', 'identity', 'exogenous', 'exogenous_tcn', 'exogenous_wavenet'].
            n_blocks: List[int]
                Number of blocks for each stack.
                Note that len(n_blocks) = len(stack_types).
            n_layers: List[int]
                Number of layers for each stack type.
                Note that len(n_layers) = len(stack_types).
            n_mlp_units: List[List[int]]
                Structure of hidden layers for each stack type.
                Each internal list should contain the number of units of each hidden layer.
                Note that len(n_hidden) = len(stack_types).
            n_x_hidden: int
                Number of hidden output channels of exogenous_tcn and exogenous_wavenet stacks.
            n_s_hidden: int
                Number of encoded static features, output dim of _StaticFeaturesEncoder.
            n_pool_kernel_size List[int]:
                Pooling size for input for each stack.
                Note that len(n_pool_kernel_size) = len(stack_types).
            n_freq_downsample List[int]:
                Downsample multiplier of output for each stack. Expressivity ratio (r) = 1/n_freq_downsample
                Note that len(n_freq_downsample) = len(stack_types).
            pooling_mode: str
                Pooling type.
                An item from ['average', 'max']
            interpolation_mode: str
                Interpolation function.
                An item from ['linear', 'nearest', 'cubic']
            batch_normalization: bool
                Whether perform batch normalization.
            dropout_prob_theta: float
                Float between (0, 1).
                Dropout for Nbeats basis.
            learning_rate: float
                Learning rate between (0, 1).
            lr_decay: float
                Decreasing multiplier for the learning rate.
            lr_decay_step_size: int
                Steps between each learning rate decay.
            weight_decay: float
                L2 penalty for optimizer.
            loss_train: str
                Loss to optimize.
                An item from ['MAPE', 'MASE', 'SMAPE', 'MSE', 'MAE', 'QUANTILE', 'QUANTILE2'].
            loss_hypar: float
                Hyperparameter for chosen loss.
            loss_valid: str
                Validation loss.
                An item from ['MAPE', 'MASE', 'SMAPE', 'RMSE', 'MAE', 'QUANTILE'].
            frequency: str
                Time series frequency.
            random_seed: int
                random_seed for pseudo random pytorch initializer and
                numpy random generator.
        """

        super(NHITS, self).__init__()

        if activation == 'SELU': initialization = 'lecun_normal'

        #------------------------ Model Attributes ------------------------#
        # Architecture parameters
        self.n_time_in = n_time_in
        self.n_time_out = n_time_out
        self.n_x = n_x
        self.n_x_hidden = n_x_hidden
        self.n_s = n_s
        self.n_s_hidden = n_s_hidden
        self.shared_weights = shared_weights
        self.activation = activation
        self.initialization = initialization
        self.stack_types = stack_types
        self.n_blocks = n_blocks
        self.n_layers = n_layers
        self.n_mlp_units = n_mlp_units
        self.n_pool_kernel_size = n_pool_kernel_size
        self.n_freq_downsample = n_freq_downsample
        self.pooling_mode = pooling_mode
        self.interpolation_mode = interpolation_mode

        # Loss functions
        self.loss_train = loss_train
        self.loss_hypar = loss_hypar
        self.loss_valid = loss_valid
        self.loss_fn_train = LossFunction(loss_train,
                                          percentile=self.loss_hypar,
                                          seasonality=self.loss_hypar)
        self.loss_fn_valid = LossFunction(loss_valid,
                                          percentile=self.loss_hypar,
                                          seasonality=self.loss_hypar)

        # Regularization and optimization parameters
        self.batch_normalization = batch_normalization
        self.dropout_prob_theta = dropout_prob_theta
        self.learning_rate = learning_rate
        self.lr_decay = lr_decay
        self.weight_decay = weight_decay
        self.lr_decay_step_size = lr_decay_step_size
        self.random_seed = random_seed

        # Data parameters
        self.frequency = frequency
        self.return_decomposition = False

        self.model = _NHITS(n_time_in=self.n_time_in,
                            n_time_out=self.n_time_out,
                            n_s=self.n_s,
                            n_x=self.n_x,
                            n_s_hidden=self.n_s_hidden,
                            n_x_hidden=self.n_x_hidden,
                            stack_types=self.stack_types,
                            n_blocks=self.n_blocks,
                            n_layers=self.n_layers,
                            n_mlp_units=self.n_mlp_units,
                            n_pool_kernel_size=self.n_pool_kernel_size,
                            n_freq_downsample=self.n_freq_downsample,
                            pooling_mode=self.pooling_mode,
                            interpolation_mode=self.interpolation_mode,
                            dropout_prob_theta=self.dropout_prob_theta,
                            activation=self.activation,
                            initialization=self.initialization,
                            batch_normalization=self.batch_normalization,
                            shared_weights=self.shared_weights)

    def training_step(self, batch, batch_idx):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)

        loss = self.loss_fn_train(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('train_loss', loss, prog_bar=True, on_epoch=True)

        return loss

    def validation_step(self, batch, idx):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)

        loss = self.loss_fn_valid(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('val_loss', loss, prog_bar=True)

        return loss

    def on_fit_start(self):
        t.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)
        random.seed(self.random_seed) #TODO: interaccion rara con window_sampling de validacion

    def forward(self, batch):
        S = batch['S']
        Y = batch['Y']
        X = batch['X']
        sample_mask = batch['sample_mask']
        available_mask = batch['available_mask']

        if self.return_decomposition:
            outsample_y, forecast, block_forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                                     insample_mask=available_mask,
                                                                     outsample_mask=sample_mask,
                                                                     return_decomposition=True)
            return outsample_y, forecast, block_forecast, outsample_mask

        outsample_y, forecast, outsample_mask = self.model(S=S, Y=Y, X=X,
                                                           insample_mask=available_mask,
                                                           outsample_mask=sample_mask,
                                                           return_decomposition=False)
        return outsample_y, forecast, outsample_mask

    def configure_optimizers(self):
        optimizer = optim.Adam(self.model.parameters(),
                               lr=self.learning_rate,
                               weight_decay=self.weight_decay)

        lr_scheduler = optim.lr_scheduler.StepLR(optimizer,
                                                 step_size=self.lr_decay_step_size,
                                                 gamma=self.lr_decay)

        return {'optimizer': optimizer, 'lr_scheduler': lr_scheduler}


# Cell
@patch
def forecast(self: NHITS, Y_df: pd.DataFrame, X_df: pd.DataFrame = None, S_df: pd.DataFrame = None,
                batch_size: int =1, trainer: pl.Trainer =None) -> pd.DataFrame:
    """
    Method for forecasting self.n_time_out periods after last timestamp of Y_df.

    Parameters
    ----------
    Y_df: pd.DataFrame
        Dataframe with target time-series data, needs 'unique_id','ds' and 'y' columns.
    X_df: pd.DataFrame
        Dataframe with exogenous time-series data, needs 'unique_id' and 'ds' columns.
        Note that 'unique_id' and 'ds' must match Y_df plus the forecasting horizon.
    S_df: pd.DataFrame
        Dataframe with static data, needs 'unique_id' column.
    bath_size: int
        Batch size for forecasting.
    trainer: pl.Trainer
        Trainer object for model training and evaluation.

    Returns
    ----------
    forecast_df: pd.DataFrame
        Dataframe with forecasts.
    """

    # Add forecast dates to Y_df
    Y_df['ds'] = pd.to_datetime(Y_df['ds'])
    if X_df is not None:
        X_df['ds'] = pd.to_datetime(X_df['ds'])
    forecast_dates = pd.date_range(Y_df['ds'].max(), periods=self.n_time_out+1, freq=self.frequency)[1:]
    index = pd.MultiIndex.from_product([Y_df['unique_id'].unique(), forecast_dates], names=['unique_id', 'ds'])
    forecast_df = pd.DataFrame({'y':[0]}, index=index).reset_index()

    Y_df = Y_df.append(forecast_df).sort_values(['unique_id','ds']).reset_index(drop=True)

    # Dataset, loader and trainer
    dataset = WindowsDataset(S_df=S_df, Y_df=Y_df, X_df=X_df,
                                mask_df=None, f_cols=[],
                                input_size=self.n_time_in,
                                output_size=self.n_time_out,
                                sample_freq=1,
                                complete_windows=True,
                                ds_in_test=self.n_time_out,
                                is_test=True,
                                verbose=True)

    loader = TimeSeriesLoader(dataset=dataset,
                                batch_size=batch_size,
                                shuffle=False)

    if trainer is None:
        gpus = -1 if t.cuda.is_available() else 0
        trainer = pl.Trainer(progress_bar_refresh_rate=1,
                             gpus=gpus,
                             logger=False)

    # Forecast
    outputs = trainer.predict(self, loader)

    # Process forecast and include in forecast_df
    _, forecast, _ = [t.cat(output).cpu().numpy() for output in zip(*outputs)]
    forecast_df['y'] = forecast.flatten()

    return forecast_df


# Cell
def suggested_space(n_time_out: int, n_series: int, n_x: int, n_s: int,
                    frequency: str) -> dict:
    """
    Suggested hyperparameters search space for tuning. To be used with hyperopt library.

    Parameters
    ----------
    n_time_out: int
        Forecasting horizon.
    n_series: int
        Number of time-series.
    n_x: int
        Number of exogenous variables.
    n_s: int
        Number of static variables.
    frequency: str
        Frequency of time-seris.

    Returns
    ----------
    space: Dict
        Dictionary with search space for hyperopt library.
    """

    space= {# Architecture parameters
        'model':'nhits',
        'mode': 'simple',
        'n_time_in': hp.choice('n_time_in', [2*n_time_out, 3*n_time_out, 5*n_time_out]),
        'n_time_out': hp.choice('n_time_out', [n_time_out]),
        'n_x': hp.choice('n_x', [n_x]),
        'n_s': hp.choice('n_s', [n_s]),
        'shared_weights': hp.choice('shared_weights', [False]),
        'activation': hp.choice('activation', ['ReLU']),
        'initialization':  hp.choice('initialization', ['lecun_normal']),
        'stack_types': hp.choice('stack_types', [ 3*['identity'] ]),
        'constant_n_blocks': hp.choice('n_blocks', [ 1, 3 ]), # Constant n_blocks across stacks
        'constant_n_layers': hp.choice('n_layers', [ 2, 3 ]), # Constant n_layers across stacks
        'constant_n_mlp_units': hp.choice('n_mlp_units', [ 128, 256, 512, 1024 ]), # Constant n_mlp_units across stacks
        'n_x_hidden': hp.choice('n_x_hidden', [n_x] ),
        'n_s_hidden': hp.choice('n_s_hidden', [n_s] ),
        'n_pool_kernel_size': hp.choice('n_pool_kernel_size', [ 3*[1], 3*[2], 3*[4], 3*[8], [8, 4, 1], [16, 8, 1] ]),
        'n_freq_downsample': hp.choice('n_freq_downsample', [ [168, 24, 1], [24, 12, 1],
                                                                [180, 60, 1], [60, 8, 1],
                                                                [40, 20, 1] ]),
        'pooling_mode': hp.choice('pooling_mode', [ 'max' ]),
        'interpolation_mode': hp.choice('interpolation_mode', [ 'linear' ]),
        # Regularization and optimization parameters
        'batch_normalization': hp.choice('batch_normalization', [False]),
        'dropout_prob_theta': hp.choice('dropout_prob_theta', [ 0 ]),
        'learning_rate': hp.choice('learning_rate', [0.0001, 0.001, 0.005, 0.01]),
        'lr_decay': hp.choice('lr_decay', [0.5] ),
        'n_lr_decays': hp.choice('n_lr_decays', [3]),
        'weight_decay': hp.choice('weight_decay', [0] ),
        'max_epochs': hp.choice('max_epochs', [None]),
        'max_steps': hp.choice('max_steps', [1_000, 3_000, 5_000]),
        'early_stop_patience': hp.choice('early_stop_patience', [10]),
        'eval_freq': hp.choice('eval_freq', [50]),
        'loss_train': hp.choice('loss', ['MAE']),
        'loss_hypar': hp.choice('loss_hypar', [0.5]),
        'loss_valid': hp.choice('loss_valid', ['MAE']),
        # Data parameters
        'normalizer_y': hp.choice('normalizer_y', [None]),
        'normalizer_x': hp.choice('normalizer_x', [None]),
        'complete_windows':  hp.choice('complete_windows', [True]),
        'frequency': hp.choice('frequency', [frequency]),
        'idx_to_sample_freq': hp.choice('idx_to_sample_freq', [1]),
        'val_idx_to_sample_freq': hp.choice('val_idx_to_sample_freq', [1]),
        'batch_size': hp.choice('batch_size', [1]),
        'n_windows': hp.choice('n_windows', [32, 64, 128, 256, 512]),
        'random_seed': hp.quniform('random_seed', 1, 20, 1)}

    return space