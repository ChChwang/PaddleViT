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

"""
Implement Transformer Class for BoTNet
"""

import paddle
import paddle.nn as nn
import paddlenlp
from einops import rearrange
from resnet import resnet50

def expand_dim(t, dim, k):
    """
    Expand dims for t at dim to k
    """
    t = t.unsqueeze(axis=dim)
    expand_shape = [-1] * len(t.shape)
    expand_shape[dim] = k
    return paddle.expand(t, expand_shape)

def rel_to_abs(x):
    """
    x_tensor: [B, Nh * H, L, 2L - 1]
    Convert relative position between the key and query to their absolute position respectively.
    Tensowflow source code in the appendix of: https://arxiv.org/pdf/1904.09925.pdf
    """
    B, Nh, L, _ = x.shape
    # pad to shift from relative to absolute indexing
    col_pad = paddle.zeros([B, Nh, L, 1])
    x = paddle.concat([x, col_pad], axis=3)
    flat_x = paddle.reshape(x, [B, Nh, L * 2 * L])
    flat_pad = paddle.zeros([B, Nh, L - 1])
    flat_x = paddle.concat([flat_x, flat_pad], axis=2)
    # Reshape and slice out the padded elements
    final_x = paddle.reshape(flat_x, [B, Nh, L + 1, 2 * L - 1])
    return final_x[:, :, :L, L - 1 :]

def relative_logits_1d(q, rel_k):
    """
    q: [B, Nh, H, W, d]
    rel_k: [2W - 1, d]
    Computes relative logits along one dimension.
    The details of relative position is explained in: https://arxiv.org/pdf/1803.02155.pdf
    """
    B, Nh, H, W, _ = q.shape
    rel_logits = paddlenlp.ops.einsum("b n h w d, m d -> b n h w m", q, rel_k)
    # Collapse height and heads
    rel_logits = paddle.reshape(rel_logits, [-1, Nh * H, W, 2 * W - 1])
    rel_logits = rel_to_abs(rel_logits)
    rel_logits = paddle.reshape(rel_logits, [-1, Nh, H, W, W])
    rel_logits = expand_dim(rel_logits, dim=3, k=H)
    return rel_logits

class RelPosEmb(nn.Layer):
    """
    relative position embedding
    """
    def __init__(self,
                 height,
                 width,
                 dim_head):
        super().__init__()
        scale = dim_head ** -0.5
        self.height = height
        self.width = width
        tmp_h = paddle.randn(shape=[height * 2 - 1, dim_head]) * scale
        tmp_w = paddle.randn(shape=[width * 2 - 1, dim_head]) * scale
        self.rel_height = paddle.create_parameter(
            tmp_h.shape, dtype='float32', default_initializer=paddle.nn.initializer.Assign(tmp_h)
        )
        self.rel_width = paddle.create_parameter(
            tmp_w.shape, dtype='float32', default_initializer=paddle.nn.initializer.Assign(tmp_w)
        )

    def forward(self, q):
        h = self.height
        w = self.width
        tmp_q = q.numpy()
        q = paddle.to_tensor(rearrange(tmp_q, "b h (x y) d -> b h x y d", x=h, y=w))
        rel_logits_w = relative_logits_1d(q, self.rel_width)
        tmp_w = rel_logits_w.numpy()
        rel_logits_w = paddle.to_tensor(rearrange(tmp_w, "b h x i y j-> b h (x y) (i j)"))

        tmp_q = q.numpy()
        q = paddle.to_tensor(rearrange(tmp_q, "b h x y d -> b h y x d"))
        rel_logits_h = relative_logits_1d(q, self.rel_height)
        tmp_h = rel_logits_h.numpy()
        rel_logits_h = paddle.to_tensor(rearrange(tmp_h, "b h x i y j -> b h (y x) (j i)"))
        return rel_logits_w + rel_logits_h

class BoTBlock(nn.Layer):
    """
    botblock
    """
    def __init__(self,
                 dim,
                 fmap_size,
                 dim_out,
                 stride=1,
                 heads=4,
                 proj_factor=4,
                 dim_qk=128,
                 dim_v=128,
                 rel_pos_emb=False,
                 activation=nn.ReLU(),):
        super().__init__()
        if dim != dim_out or stride != 1:
            self.shortcut = nn.Sequential(
                nn.Conv2D(dim, dim_out, kernel_size=1, stride=stride, bias_attr=False),
                nn.BatchNorm2D(dim_out, momentum=0.1),
                activation,
            )
        else:
            self.shortcut = nn.Identity()

        bottleneck_dimension = dim_out // proj_factor  # from 2048 to 512
        attn_dim_out = heads * dim_v

        self.net = nn.Sequential(
            nn.Conv2D(dim, bottleneck_dimension, kernel_size=1, stride=1, bias_attr=False),
            nn.BatchNorm2D(bottleneck_dimension, momentum=0.1),
            activation,
            MHSA(
                dim=bottleneck_dimension,
                fmap_size=fmap_size,
                heads=heads,
                dim_qk=dim_qk,
                dim_v=dim_v,
                rel_pos_emb=rel_pos_emb,
            ),
            nn.AvgPool2D(2) if stride == 2 else nn.Identity(),  # same padding
            nn.BatchNorm2D(attn_dim_out, momentum=0.1),
            activation,
            nn.Conv2D(attn_dim_out, dim_out, kernel_size=1, stride=1, bias_attr=False),
            nn.BatchNorm2D(dim_out, momentum=0.1),
        )

        self.activation = activation

    def forward(self, featuremap):
        shortcut = self.shortcut(featuremap)
        featuremap = self.net(featuremap)
        featuremap += shortcut
        return self.activation(featuremap)

class MHSA(nn.Layer):
    """
    The details of relative position is explained in: https://arxiv.org/abs/2101.11605v1
    """
    def __init__(self,
                 dim,
                 fmap_size,
                 heads=4,
                 dim_qk=128,
                 dim_v=128,
                 rel_pos_emb=False):
        super().__init__()
        self.scale = dim_qk ** -0.5
        self.heads = heads
        out_channels_qk = heads * dim_qk
        out_channels_v = heads * dim_v

        self.to_qk = nn.Conv2D(dim, out_channels_qk * 2, 1, bias_attr=False)
        self.to_v = nn.Conv2D(dim, out_channels_v, 1, bias_attr=False)
        self.softmax = nn.Softmax(axis=-1)

        height, width = fmap_size
        self.pos_emb = RelPosEmb(height, width, dim_qk)

    def forward(self, featuremap):
        heads = self.heads
        B, C, H, W = featuremap.shape
        q, k = self.to_qk(featuremap).chunk(2, axis=1)
        v = self.to_v(featuremap)
        tmp_q, tmp_k, tmp_v = q.numpy(), k.numpy(), v.numpy()
        q, k, v = map(
            lambda x: paddle.to_tensor(rearrange(x, "B (h d) H W -> B h (H W) d", h=heads)),
            (tmp_q, tmp_k, tmp_v)
        )

        q *= self.scale

        logits = paddlenlp.ops.einsum("b h x d, b h y d -> b h x y", q, k)
        logits += self.pos_emb(q)

        weights = self.softmax(logits)
        attn_out = paddlenlp.ops.einsum("b h x y, b h y d -> b h x d", weights, v)
        tmp_out = attn_out.numpy()
        attn_out = paddle.to_tensor(rearrange(tmp_out, "B h (H W) d -> B (h d) H W", H=H))
        return attn_out

class BoTStack(nn.Layer):
    """
    botstack
    """
    def __init__(self,
                 dim,
                 fmap_size,
                 dim_out=2048,
                 heads=4,
                 proj_factor=4,
                 num_layers=3,
                 stride=2,
                 dim_qk=128,
                 dim_v=128,
                 rel_pos_emb=False,
                 activation=nn.ReLU(),):
        super().__init__()

        self.dim = dim
        self.fmap_size = fmap_size

        layers = []

        for i in range(num_layers):
            is_first = i == 0
            dim = dim if is_first else dim_out

            fmap_divisor = 2 if stride == 2 and not is_first else 1
            layer_fmap_size = tuple(map(lambda t: t // fmap_divisor, fmap_size))

            layers.append(
                BoTBlock(
                    dim=dim,
                    fmap_size=layer_fmap_size,
                    dim_out=dim_out,
                    stride=stride if is_first else 1,
                    heads=heads,
                    proj_factor=proj_factor,
                    dim_qk=dim_qk,
                    dim_v=dim_v,
                    rel_pos_emb=rel_pos_emb,
                    activation=activation,
                )
            )

        self.net = nn.Sequential(*layers)

    def forward(self, x):
        _, c, h, w = x.shape
        assert c == self.dim, f"assert {c} == self.dim {self.dim}"
        assert h == self.fmap_size[0] and w == self.fmap_size[1]
        return self.net(x)

def botnet50(
        image_size=224,
        fmap_size=(14, 14),
        num_classes=1000,
        embed_dim=2048,
        pretrained=False,
        **kwargs):
    resnet = resnet50(pretrained=False, **kwargs)
    layer = BoTStack(dim=1024, dim_out=embed_dim, fmap_size=fmap_size, stride=1, rel_pos_emb=True)
    backbone = list(resnet.children())
    model = nn.Sequential(
        *backbone[:-3],
        layer,
        nn.AdaptiveAvgPool2D([1, 1]),
        nn.Flatten(1),
        nn.Linear(embed_dim, num_classes),
    )
    if pretrained:
        state_dict = paddle.load('botnet50.pdparams')
        model.set_state_dict(state_dict)
    return model

def build_botnet50(config):
    model = botnet50(
        image_size=config.DATA.IMAGE_SIZE,
        fmap_size=config.DATA.FMAP_SIZE,
        num_classes=config.MODEL.NUM_CLASSES,
        embed_dim=config.MODEL.TRANS.EMBED_DIM,
        pretrained=config.MODEL.PRETRAINED,
    )
    return model
