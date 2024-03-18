#!/usr/bin/env python

import logging
import math

# import _ext as _backend
import torch
from torch import nn
from torch.autograd import Function
from torch.autograd.function import once_differentiable
from torch.nn.modules.utils import _pair
import pdb
import torch.utils.checkpoint as checkpoint
from mmcv.ops import ModulatedDeformConv2d, modulated_deform_conv2d

logger = logging.getLogger('base')

"""
class _DCNv2(Function):

    @staticmethod
    def forward(ctx, input, offset, mask, weight, bias, stride, padding,
                dilation, deformable_groups):
        ctx.stride = _pair(stride)
        ctx.padding = _pair(padding)
        ctx.dilation = _pair(dilation)
        ctx.kernel_size = _pair(weight.shape[2:4])
        ctx.deformable_groups = deformable_groups
        output = _backend.dcn_v2_forward(
            input, weight, bias, offset, mask, ctx.kernel_size[0],
            ctx.kernel_size[1], ctx.stride[0], ctx.stride[1], ctx.padding[0],
            ctx.padding[1], ctx.dilation[0], ctx.dilation[1],
            ctx.deformable_groups)
        ctx.save_for_backward(input, offset, mask, weight, bias)
        return output

    @staticmethod
    @once_differentiable
    def backward(ctx, grad_output):
        input, offset, mask, weight, bias = ctx.saved_tensors
        grad_input, grad_offset, grad_mask, grad_weight, grad_bias = \
            _backend.dcn_v2_backward(input, weight,
                                     bias,
                                     offset, mask,
                                     grad_output,
                                     ctx.kernel_size[0], ctx.kernel_size[1],
                                     ctx.stride[0], ctx.stride[1],
                                     ctx.padding[0], ctx.padding[1],
                                     ctx.dilation[0], ctx.dilation[1],
                                     ctx.deformable_groups)

        return grad_input, grad_offset, grad_mask, grad_weight, grad_bias,\
            None, None, None, None,


dcn_v2_conv = _DCNv2.apply
"""


class DCNv2(nn.Module):

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1):
        super(DCNv2, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = _pair(kernel_size)
        self.stride = _pair(stride)
        self.padding = _pair(padding)
        self.dilation = _pair(dilation)
        self.deformable_groups = deformable_groups

        self.weight = nn.Parameter(
            torch.Tensor(out_channels, in_channels, *self.kernel_size))
        self.bias = nn.Parameter(torch.Tensor(out_channels))
        self.reset_parameters()

    def reset_parameters(self):
        n = self.in_channels
        for k in self.kernel_size:
            n *= k
        stdv = 1. / math.sqrt(n)
        self.weight.data.uniform_(-stdv, stdv)
        self.bias.data.zero_()

    def forward(self, input, offset, mask):
        assert 2 * self.deformable_groups * self.kernel_size[
            0] * self.kernel_size[1] == offset.shape[1]
        assert self.deformable_groups * self.kernel_size[0] * self.kernel_size[
            1] == mask.shape[1]
        return modulated_deform_conv2d(input, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class DCN(DCNv2):

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1):
        super(DCN, self).__init__(in_channels, out_channels, kernel_size,
                                  stride, padding, dilation, deformable_groups)

        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, input):
        out = self.conv_offset_mask(input)
        o1, o2, mask = torch.chunk(out, 3, dim=1)
        offset = torch.cat((o1, o2), dim=1)
        mask = torch.sigmoid(mask)
        return modulated_deform_conv2d(input, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class DCN_sep(DCNv2):
    '''Use other features to generate offsets and masks'''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True):
        super(DCN_sep,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x):
        if self.extra_offset_mask:
            # x = [input, features]
            out = self.conv_offset_mask(x[1])
            x = x[0]
        else:
            out = self.conv_offset_mask(x)
        o1, o2, mask = torch.chunk(out, 3, dim=1)
        offset = torch.cat((o1, o2), dim=1)
        mask = torch.sigmoid(mask)

        offset_mean = torch.mean(torch.abs(offset))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class FlowGuidedDCN(DCNv2):
    '''Use other features to generate offsets and masks'''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1):
        super(FlowGuidedDCN, self).__init__(in_channels, out_channels, kernel_size, stride, padding,
                                            dilation, deformable_groups)

        channels_ = self.deformable_groups * 3 * self.kernel_size[0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            in_channels, channels_, kernel_size, stride, padding, bias=True)

        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, input, fea, flows):
        '''input: input features for deformable conv: N, C, H, W.
           fea: other features used for generating offsets and mask: N, C, H, W.
           flows: N, 2, H, W.
        '''
        out = self.conv_offset_mask(fea)
        o1, o2, mask = torch.chunk(out, 3, dim=1)

        offset = torch.tanh(torch.cat((o1, o2), dim=1)) * 10  # max_residue_magnitude
        offset = offset + flows.flip(1).repeat(1, offset.size(1) // 2, 1, 1)

        offset_mean = torch.mean(torch.abs(offset))
        if offset_mean > 250:
            print('FlowGuidedDCN: Offset mean is {}, larger than 100.'.format(offset_mean))
            # offset = offset.clamp(-50, 50)
            # return None

        mask = torch.sigmoid(mask)
        return modulated_deform_conv2d(input, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding,
                                       self.dilation, 1, self.deformable_groups)


class DCN_sep_pre_multi_offset(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True):
        super(DCN_sep_pre_multi_offset,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]
        # print(offset.size())
        mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))

        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


# TODO cpu version
class DCN_sep_pre_multi_offset_cpu(DCNv2):
    '''
    Use other features to generate offsets and masks.
    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True):
        super(DCN_sep_pre_multi_offset_cpu,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]
        # print(offset.size())
        mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding,
                                       self.dilation, 1, self.deformable_groups)


class DCN_sep_pre_multi_offset_v2(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True):
        super(DCN_sep_pre_multi_offset_v2,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]
        mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return offset, modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                               self.stride, self.padding, self.dilation, 1,
                                               self.deformable_groups)


class DCN_sep_pre_multi_offset_v2_1(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True):
        super(DCN_sep_pre_multi_offset_v2_1,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        # pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  #[9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        offset = offset + pre_offset  # [9, 144, 40, 40]
        mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class DCN_sep_pre_multi_offset_flow_similarity(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True,
                 max_residue_magnitude=10,
                 use_sim=True
                 ):
        super(DCN_sep_pre_multi_offset_flow_similarity,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        self.max_residue_magnitude = max_residue_magnitude
        self.use_sim = use_sim

        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]

        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)

        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset, pre_sim):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [1, 144, 128, 128]
        if self.max_residue_magnitude:
            offset = self.max_residue_magnitude * torch.tanh(offset)
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [1, 8, 128, 128, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [1, 144, 128, 128]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]

        if self.use_sim:
            mask = torch.sigmoid(mask * pre_sim)
        else:
            mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class DCN_sep_pre_multi_offset_flow_similarity_v2(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True,
                 max_residue_magnitude=10,
                 use_sim=True
                 ):
        super(DCN_sep_pre_multi_offset_flow_similarity_v2,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        self.max_residue_magnitude = max_residue_magnitude
        self.use_sim = use_sim

        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]

        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)

        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset, pre_sim):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        if self.max_residue_magnitude:
            offset = self.max_residue_magnitude * torch.tanh(offset)
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]

        if self.use_sim:
            mask = torch.sigmoid(mask * pre_sim)
        else:
            mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return offset, modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                               self.stride, self.padding, self.dilation, 1,
                                               self.deformable_groups)


class DCN_sep_pre_multi_offset_flow_similarity_v2_1(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True,
                 max_residue_magnitude=10,
                 use_sim=True
                 ):
        super(DCN_sep_pre_multi_offset_flow_similarity_v2_1,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        self.max_residue_magnitude = max_residue_magnitude
        self.use_sim = use_sim

        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]

        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)

        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset, pre_sim):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        if self.max_residue_magnitude:
            offset = self.max_residue_magnitude * torch.tanh(offset)

        offset = offset + pre_offset  # [9, 144, 40, 40]

        if self.use_sim:
            mask = torch.sigmoid(mask * pre_sim)
        else:
            mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


class DCN_sep_pre_multi_offset_flow_similarity_cpu(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True,
                 max_residue_magnitude=10,
                 use_sim=True
                 ):
        super(DCN_sep_pre_multi_offset_flow_similarity_cpu,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        self.max_residue_magnitude = max_residue_magnitude
        self.use_sim = use_sim

        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]

        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)

        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset, pre_sim):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        if self.max_residue_magnitude:
            offset = self.max_residue_magnitude * torch.tanh(offset)
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]

        if self.use_sim:
            mask = torch.sigmoid(mask * pre_sim)
        else:
            mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))

        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding,
                                       self.dilation, 1, self.deformable_groups)


class DCN_sep_pre_multi_offset_withTanh(DCNv2):
    '''
    Use other features to generate offsets and masks.

    Intialized the offset with precomputed non-local offset.
    '''

    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride,
                 padding,
                 dilation=1,
                 deformable_groups=1,
                 extra_offset_mask=True,
                 max_residue_magnitude=10,
                 ):
        super(DCN_sep_pre_multi_offset_withTanh,
              self).__init__(in_channels, out_channels, kernel_size, stride,
                             padding, dilation, deformable_groups)
        self.extra_offset_mask = extra_offset_mask
        self.max_residue_magnitude = max_residue_magnitude
        channels_ = self.deformable_groups * 3 * self.kernel_size[
            0] * self.kernel_size[1]
        self.conv_offset_mask = nn.Conv2d(
            self.in_channels,
            channels_,
            kernel_size=self.kernel_size,
            stride=self.stride,
            padding=self.padding,
            bias=True)
        self.init_offset()

    def init_offset(self):
        self.conv_offset_mask.weight.data.zero_()
        self.conv_offset_mask.bias.data.zero_()

    def forward(self, x, pre_offset):
        '''
        Args:
            pre_offset: precomputed_offset. Size: [b, 9, h, w, 2]
        '''

        if self.extra_offset_mask:
            # x = [input, features] x[1]:[9, 256, 40, 40]
            out = self.conv_offset_mask(x[1])  # [9, 216, 40, 40]
            x = x[0]  # [9, 256, 40, 40]
        else:
            out = self.conv_offset_mask(x)

        o1, o2, mask = torch.chunk(out, 3, dim=1)  # [9, 72, 40, 40]
        offset = torch.cat((o1, o2), dim=1)  # [9, 144, 40, 40]
        offset = self.max_residue_magnitude * torch.tanh(offset)  # New add
        # repeat pre_offset along dim1, shape: [b, 9*groups, h, w, 2]
        pre_offset = pre_offset.repeat([1, self.deformable_groups, 1, 1, 1])  # [9, 72, 40, 40, 2]
        # the order of offset is [y, x, y, x, ..., y, x]
        pre_offset_reorder = torch.zeros_like(offset)  # [9, 144, 40, 40]
        # add pre_offset on y-axis
        pre_offset_reorder[:, 0::2, :, :] = pre_offset[:, :, :, :, 1]
        # add pre_offset on x-axis
        pre_offset_reorder[:, 1::2, :, :] = pre_offset[:, :, :, :, 0]
        offset = offset + pre_offset_reorder  # [9, 144, 40, 40]
        # print(offset.size())
        mask = torch.sigmoid(mask)  # [9, 72, 40, 40]

        offset_mean = torch.mean(torch.abs(offset - pre_offset_reorder))
        if offset_mean > 100:
            logger.warning(
                'Offset mean is {}, larger than 100.'.format(offset_mean))
        return modulated_deform_conv2d(x, offset, mask, self.weight, self.bias,
                                       self.stride, self.padding, self.dilation, 1,
                                       self.deformable_groups)


"""
class _DCNv2Pooling(Function):

    @staticmethod
    def forward(ctx,
                input,
                rois,
                offset,
                spatial_scale,
                pooled_size,
                output_dim,
                no_trans,
                group_size=1,
                part_size=None,
                sample_per_part=4,
                trans_std=.0):
        ctx.spatial_scale = spatial_scale
        ctx.no_trans = int(no_trans)
        ctx.output_dim = output_dim
        ctx.group_size = group_size
        ctx.pooled_size = pooled_size
        ctx.part_size = pooled_size if part_size is None else part_size
        ctx.sample_per_part = sample_per_part
        ctx.trans_std = trans_std

        output, output_count = _backend.dcn_v2_psroi_pooling_forward(
            input, rois, offset, ctx.no_trans, ctx.spatial_scale,
            ctx.output_dim, ctx.group_size, ctx.pooled_size, ctx.part_size,
            ctx.sample_per_part, ctx.trans_std)
        ctx.save_for_backward(input, rois, offset, output_count)
        return output

    @staticmethod
    @once_differentiable
    def backward(ctx, grad_output):
        input, rois, offset, output_count = ctx.saved_tensors
        grad_input, grad_offset = \
            _backend.dcn_v2_psroi_pooling_backward(grad_output,
                                                   input,
                                                   rois,
                                                   offset,
                                                   output_count,
                                                   ctx.no_trans,
                                                   ctx.spatial_scale,
                                                   ctx.output_dim,
                                                   ctx.group_size,
                                                   ctx.pooled_size,
                                                   ctx.part_size,
                                                   ctx.sample_per_part,
                                                   ctx.trans_std)

        return grad_input, None, grad_offset, \
            None, None, None, None, None, None, None, None


dcn_v2_pooling = _DCNv2Pooling.apply


class DCNv2Pooling(nn.Module):

    def __init__(self,
                 spatial_scale,
                 pooled_size,
                 output_dim,
                 no_trans,
                 group_size=1,
                 part_size=None,
                 sample_per_part=4,
                 trans_std=.0):
        super(DCNv2Pooling, self).__init__()
        self.spatial_scale = spatial_scale
        self.pooled_size = pooled_size
        self.output_dim = output_dim
        self.no_trans = no_trans
        self.group_size = group_size
        self.part_size = pooled_size if part_size is None else part_size
        self.sample_per_part = sample_per_part
        self.trans_std = trans_std

    def forward(self, input, rois, offset):
        assert input.shape[1] == self.output_dim
        if self.no_trans:
            offset = input.new()
        return dcn_v2_pooling(input, rois, offset, self.spatial_scale,
                              self.pooled_size, self.output_dim, self.no_trans,
                              self.group_size, self.part_size,
                              self.sample_per_part, self.trans_std)


class DCNPooling(DCNv2Pooling):

    def __init__(self,
                 spatial_scale,
                 pooled_size,
                 output_dim,
                 no_trans,
                 group_size=1,
                 part_size=None,
                 sample_per_part=4,
                 trans_std=.0,
                 deform_fc_dim=1024):
        super(DCNPooling,
              self).__init__(spatial_scale, pooled_size, output_dim, no_trans,
                             group_size, part_size, sample_per_part, trans_std)

        self.deform_fc_dim = deform_fc_dim

        if not no_trans:
            self.offset_mask_fc = nn.Sequential(
                nn.Linear(
                    self.pooled_size * self.pooled_size * self.output_dim,
                    self.deform_fc_dim), nn.ReLU(inplace=True),
                nn.Linear(self.deform_fc_dim, self.deform_fc_dim),
                nn.ReLU(inplace=True),
                nn.Linear(self.deform_fc_dim,
                          self.pooled_size * self.pooled_size * 3))
            self.offset_mask_fc[4].weight.data.zero_()
            self.offset_mask_fc[4].bias.data.zero_()

    def forward(self, input, rois):
        offset = input.new()

        if not self.no_trans:

            # do roi_align first
            n = rois.shape[0]
            roi = dcn_v2_pooling(
                input,
                rois,
                offset,
                self.spatial_scale,
                self.pooled_size,
                self.output_dim,
                True,  # no trans
                self.group_size,
                self.part_size,
                self.sample_per_part,
                self.trans_std)

            # build mask and offset
            offset_mask = self.offset_mask_fc(roi.view(n, -1))
            offset_mask = offset_mask.view(n, 3, self.pooled_size,
                                           self.pooled_size)
            o1, o2, mask = torch.chunk(offset_mask, 3, dim=1)
            offset = torch.cat((o1, o2), dim=1)
            mask = torch.sigmoid(mask)

            # do pooling with offset and mask
            return dcn_v2_pooling(
                input, rois, offset, self.spatial_scale, self.pooled_size,
                self.output_dim, self.no_trans, self.group_size,
                self.part_size, self.sample_per_part, self.trans_std) * mask
        # only roi_align
        return dcn_v2_pooling(input, rois, offset, self.spatial_scale,
                              self.pooled_size, self.output_dim, self.no_trans,
                              self.group_size, self.part_size,
                              self.sample_per_part, self.trans_std)
"""
