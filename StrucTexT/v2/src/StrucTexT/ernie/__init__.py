""" __init__.py """
#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import paddle
if paddle.__version__ != '0.0.0' and paddle.__version__ < '2.1.0':
    raise RuntimeError('propeller 0.2 requires paddle 2.1+, got %s' %
                       paddle.__version__)

from .tokenizing_ernie import ErnieTokenizer
from .modeling_ernie import ErnieModel, ErnieEncoderStack
from .modeling_ernie import append_name
from .modeling_ernie import _build_linear as build_linear
from .modeling_ernie import _build_ln as build_ln
from .modeling_ernie import _get_rel_pos_bias as get_rel_pos_bias
