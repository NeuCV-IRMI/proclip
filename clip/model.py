from collections import OrderedDict
from typing import Tuple, Union

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1):
        super().__init__()

        self.conv1 = nn.Conv2d(inplanes, planes, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)

        self.conv2 = nn.Conv2d(planes, planes, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.avgpool = nn.AvgPool2d(stride) if stride > 1 else nn.Identity()

        self.conv3 = nn.Conv2d(planes, planes * self.expansion, 1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)

        self.relu = nn.ReLU(inplace=True)
        self.downsample = None
        self.stride = stride

        if stride > 1 or inplanes != planes * Bottleneck.expansion:
            self.downsample = nn.Sequential(OrderedDict([
                ("-1", nn.AvgPool2d(stride)),
                ("0", nn.Conv2d(inplanes, planes * self.expansion, 1, stride=1, bias=False)),
                ("1", nn.BatchNorm2d(planes * self.expansion))
            ]))

    def forward(self, x: torch.Tensor):
        identity = x

        out = self.relu(self.bn1(self.conv1(x)))
        out = self.relu(self.bn2(self.conv2(out)))
        out = self.avgpool(out)
        out = self.bn3(self.conv3(out))

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)
        return out


class AttentionPool2d(nn.Module):
    def __init__(self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int = None):
        super().__init__()
        self.positional_embedding = nn.Parameter(torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5)
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
        self.num_heads = num_heads

    def forward(self, x):
        x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3]).permute(2, 0, 1)
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)
        x = x + self.positional_embedding[:, None, :].to(x.dtype)
        x, _ = F.multi_head_attention_forward(
            query=x, key=x, value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False
        )

        return x[0]


class ModifiedResNet(nn.Module):
    """
    A ResNet class that is similar to torchvision's but contains the following changes:
    - There are now 3 "stem" convolutions as opposed to 1, with an average pool instead of a max pool.
    - Performs anti-aliasing strided convolutions, where an avgpool is prepended to convolutions with stride > 1
    - The final pooling layer is a QKV attention instead of an average pool
    """

    def __init__(self, layers, output_dim, heads, input_resolution=224, width=64):
        super().__init__()
        self.output_dim = output_dim
        self.input_resolution = input_resolution

        self.conv1 = nn.Conv2d(3, width // 2, kernel_size=3, stride=2, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(width // 2)
        self.conv2 = nn.Conv2d(width // 2, width // 2, kernel_size=3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(width // 2)
        self.conv3 = nn.Conv2d(width // 2, width, kernel_size=3, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(width)
        self.avgpool = nn.AvgPool2d(2)
        self.relu = nn.ReLU(inplace=True)

        self._inplanes = width
        self.layer1 = self._make_layer(width, layers[0])
        self.layer2 = self._make_layer(width * 2, layers[1], stride=2)
        self.layer3 = self._make_layer(width * 4, layers[2], stride=2)
        self.layer4 = self._make_layer(width * 8, layers[3], stride=2)

        embed_dim = width * 32
        self.attnpool = AttentionPool2d(input_resolution // 32, embed_dim, heads, output_dim)

    def _make_layer(self, planes, blocks, stride=1):
        layers = [Bottleneck(self._inplanes, planes, stride)]

        self._inplanes = planes * Bottleneck.expansion
        for _ in range(1, blocks):
            layers.append(Bottleneck(self._inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        def stem(x):
            for conv, bn in [(self.conv1, self.bn1), (self.conv2, self.bn2), (self.conv3, self.bn3)]:
                x = self.relu(bn(conv(x)))
            x = self.avgpool(x)
            return x

        x = x.type(self.conv1.weight.dtype)
        x = stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.attnpool(x)

        return x


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)


class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.attn_mask = attn_mask

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, x: torch.Tensor):
        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class ResidualAttentionBlock_IVLP(nn.Module):
    def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None, add_prompt=False,
                 text_layer=False, i=0, design_details=None):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.text_layer = text_layer
        self.attn_mask = attn_mask
        if i != 0:
            self.add_prompt = add_prompt
            if self.add_prompt:
                if self.text_layer:
                    self.n_ctx_text = design_details["language_ctx"]
                    ctx_vectors = torch.empty(self.n_ctx_text, d_model)
                else:
                    self.n_ctx_visual = design_details["vision_ctx"]
                    ctx_vectors = torch.empty(self.n_ctx_visual, d_model)
                nn.init.normal_(ctx_vectors, std=0.02)
                self.VPT_shallow = nn.Parameter(ctx_vectors)
        else:
            self.add_prompt = False

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, x: torch.Tensor):
        if self.add_prompt:
            if not self.text_layer:
                prefix = x[0:x.shape[0] - self.n_ctx_visual, :, :]
                visual_context = self.VPT_shallow.expand(x.shape[1], -1, -1).permute(1, 0, 2).half()
                x = torch.cat([prefix, visual_context], dim=0)
            else:
                prefix = x[:1, :, :]
                suffix = x[1 + self.n_ctx_text:, :, :]
                textual_context = self.VPT_shallow.expand(x.shape[1], -1, -1).permute(1, 0, 2).half()
                x = torch.cat([prefix, textual_context, suffix], dim=0)

        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

class ResidualAttentionBlock_PRO(nn.Module):
    def __init__(self, d_model_visual: int, n_head_visual: int, d_model_text: int, n_head_text: int, prompt_attn = None, attn_mask_visual: torch.Tensor = None,
                 attn_mask_text: torch.Tensor = None, design_details=None, i=0):
        super().__init__()
        self.compound_prompt_nctx = design_details['maple_length']
        self.parameter_sharing = design_details['parameter_sharing']

        self.visual_attn = SelfAttention(d_model_visual, n_head_visual, attn_mask_visual, False, design_details)

        self.text_attn = SelfAttention(d_model_text, n_head_text, attn_mask_text, True, design_details)

        if i == 0:
            self.first_layer = True
        else:
            self.first_layer = False

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, inputs):
        x_text = inputs[0]
        compound_prompts_deeper_text = inputs[1]
        counter_text = inputs[2]
        x_visual = inputs[3]
        compound_prompts_deeper_visual = inputs[4]
        counter_visual = inputs[5]

        if not self.first_layer:
            if len(compound_prompts_deeper_visual) > 0:
                if not (counter_visual > len(compound_prompts_deeper_visual) - 1):
                    prefix_visual = x_visual[0:x_visual.shape[0] - self.compound_prompt_nctx, :, :]
                    visual_context = compound_prompts_deeper_visual[counter_visual].half()
                    visual_context = visual_context.unsqueeze(0).expand(x_visual.shape[1] // visual_context.shape[0], -1, -1, -1).contiguous().view(-1, visual_context.shape[-2], visual_context.shape[-1]).permute(1, 0, 2)
                if not (counter_text > len(compound_prompts_deeper_text) - 1):
                    pass

            if len(compound_prompts_deeper_text) > 0:
                if not (counter_visual > len(compound_prompts_deeper_visual) - 1):
                    x_visual = torch.cat([prefix_visual, visual_context], dim=0)

                    counter_visual += 1
                if not (counter_text > len(compound_prompts_deeper_text) - 1):
                    counter_text += 1

        x_visual = self.visual_attn(x_visual)
        x_text = self.text_attn(x_text)
        return [x_text, compound_prompts_deeper_text, counter_text, x_visual, compound_prompts_deeper_visual, counter_visual]

class PromptAttention(nn.Module):
    def __init__(self, d_model_visual: int, n_head_visual: int, d_model_text: int, n_head_text: int,
                 design_details=None):
        super().__init__()
        self.promt_attention_visual = nn.MultiheadAttention(d_model_visual, n_head_visual)
        self.ln_pa_1_visual = LayerNorm(d_model_visual)
        self.mlp_pa_visual = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model_visual, d_model_visual * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model_visual * 4, d_model_visual))
        ]))
        self.ln_pa_2_visual = LayerNorm(d_model_visual)
        self.proj_v2t = nn.Linear(d_model_visual, d_model_text)
        self.proj_t2v = nn.Linear(d_model_text, d_model_visual)

        self.promt_attention_text = nn.MultiheadAttention(d_model_text, n_head_text)
        self.ln_pa_1_text = LayerNorm(d_model_text)
        self.mlp_pa_text = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model_text, d_model_text * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model_text * 4, d_model_text))
        ]))
        self.ln_pa_2_text = LayerNorm(d_model_text)

        self.fusing = design_details['fusing']

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, visual_context, textual_context):
        visual_batch = visual_context.shape[1]
        textual_batch = textual_context.shape[1]
        if self.fusing == 'first':
            visual_context = visual_context[:, 0, :].unsqueeze(1)
            textual_context = textual_context[:, 0, :].unsqueeze(1)
        elif self.fusing == 'mean':
            visual_context = torch.mean(visual_context, 1).unsqueeze(1)
            textual_context = torch.mean(textual_context, 1).unsqueeze(1)
        elif self.fusing == 'max':
            visual_context = torch.max(visual_context, 1)[0].unsqueeze(1)
            textual_context = torch.max(textual_context, 1)[0].unsqueeze(1)
        else:
            print("NO SUCH FUSING METHOD!")
        new_visual_context = visual_context + \
                             self.promt_attention_visual(self.ln_pa_1_visual(self.proj_t2v(textual_context)),
                                                         self.ln_pa_1_visual(visual_context),
                                                         self.ln_pa_1_visual(visual_context),
                                                         need_weights=False)[0]

        new_visual_context = new_visual_context + self.mlp_pa_visual(self.ln_pa_2_visual(new_visual_context))

        new_textual_context = textual_context + \
                              self.promt_attention_text(self.ln_pa_1_text(self.proj_v2t(visual_context)),
                                                        self.ln_pa_1_text(textual_context),
                                                        self.ln_pa_1_text(textual_context),
                                                        need_weights=False)[0]
        new_textual_context = new_textual_context + self.mlp_pa_text(self.ln_pa_2_text(new_textual_context))

        new_visual_context = new_visual_context.expand(-1, visual_batch, -1)
        new_textual_context = new_textual_context.expand(-1, textual_batch, -1)

        return new_visual_context, new_textual_context

class SelfAttention(nn.Module):
    def __init__(self, d_model: int, n_head: int, attn_mask: torch.Tensor = None,
                 text_layer=False, design_details=None):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)

        self.attn_mask = attn_mask
        self.compound_prompt_nctx = design_details['maple_length']
        self.text_layer = text_layer

    def forward(self, x):
        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int, attn_mask: torch.Tensor = None, prompts_needed=0,
                 text_layer=False, design_details=None):
        super().__init__()
        self.width = width
        self.layers = layers
        current_trainer = design_details['trainer']
        if current_trainer == 'IVLP':
            self.resblocks = nn.Sequential(*[ResidualAttentionBlock_IVLP(width, heads, attn_mask, True,
                                                                         text_layer, i,
                                                                         design_details) if prompts_needed > i
                                             else ResidualAttentionBlock_IVLP(width, heads, attn_mask, False,
                                                                              text_layer, i, design_details)
                                             for i in range(layers)])
        elif current_trainer == 'PRO':
            self.resblocks = nn.Sequential(
                *[ResidualAttentionBlock_PRO(width, heads, attn_mask, design_details, text_layer, i)
                  for i in range(layers)])

        else:
            assert current_trainer == 'CoOp' or current_trainer == 'CoCoOp'
            self.resblocks = nn.Sequential(*[ResidualAttentionBlock(width, heads, attn_mask) for _ in range(layers)])

    def forward(self, x: torch.Tensor):
        return self.resblocks(x)



class VisionTransformer(nn.Module):
    def __init__(self, input_resolution: int, patch_size: int, width: int, layers: int, heads: int,
                 output_dim: int, design_details):
        super().__init__()
        self.input_resolution = input_resolution
        self.output_dim = output_dim
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)
        if design_details["vision_depth"] == 0:
            self.VPT_shallow = False
        else:
            self.VPT_shallow = True
        if self.VPT_shallow:
            n_ctx = design_details["vision_ctx"]
            ctx_vectors = torch.empty(n_ctx, width)
            nn.init.normal_(ctx_vectors, std=0.02)
            self.VPT = nn.Parameter(ctx_vectors)
        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
        self.ln_pre = LayerNorm(width)
        self.prompt_till_layer_visual = design_details["vision_depth"]
        self.transformer = Transformer(width, layers, heads, prompts_needed=self.prompt_till_layer_visual,
                                       design_details=design_details)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

    def forward(self, x: torch.Tensor):
        x = self.conv1(x)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype,
                                                            device=x.device), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)

        if self.VPT_shallow:
            visual_ctx = self.VPT.expand(x.shape[0], -1, -1).half()
            x = torch.cat([x, visual_ctx], dim=1)
        else:
            assert self.prompt_till_layer_visual == 0

        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)
        x = self.transformer(x)
        x = x.permute(1, 0, 2)

        x = self.ln_post(x[:, 0, :])

        if self.proj is not None:
            x = x @ self.proj

        return x

class VisionTransformer_PRO(nn.Module):
    def __init__(self, input_resolution: int, patch_size: int, width: int, layers: int, heads: int, output_dim: int,
                 design_details):
        super().__init__()
        self.input_resolution = input_resolution
        self.output_dim = output_dim
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)
        self.VPT_shallow = True
        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
        self.ln_pre = LayerNorm(width)
        self.prompt_till_layer_visual = 0
        self.transformer = Transformer(width, layers, heads, design_details=design_details)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

    def forward(self, x: torch.Tensor, shared_ctx, compound_deeper_prompts):
        x = self.conv1(x)
        x = x.reshape(x.shape[0], x.shape[1], -1)
        x = x.permute(0, 2, 1)
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)

        if self.VPT_shallow:
            visual_ctx = shared_ctx.half()
            x = torch.cat([x, visual_ctx], dim=1)
        else:
            assert self.prompt_till_layer_visual == 0

        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)
        outputs = self.transformer([x, compound_deeper_prompts, 0])
        x = outputs[0]
        x = x.permute(1, 0, 2)

        x = self.ln_post(x)

        if self.proj is not None:
            x = x @ self.proj

        return x

class CLIP(nn.Module):
    def __init__(self,
                 embed_dim: int,
                 image_resolution: int,
                 vision_layers: Union[Tuple[int, int, int, int], int],
                 vision_width: int,
                 vision_patch_size: int,
                 context_length: int,
                 vocab_size: int,
                 transformer_width: int,
                 transformer_heads: int,
                 transformer_layers: int,
                 design_details
                 ):
        super().__init__()

        self.context_length = context_length
        trainer = design_details['trainer']
        self.transformer_width = transformer_width
        self.transformer_layers = transformer_layers
        self.input_resolution = image_resolution
        self.parameters_sharing = design_details['parameter_sharing']
        self.VPT_shallow = True
        scale = vision_width ** -0.5
        self.prompt_till_layer_visual = 0

        if design_details['trainer'] == 'PRO':
            attn_mask_text = self.build_attention_mask()
            self.class_embedding_visual = nn.Parameter(scale * torch.randn(vision_width))
            self.positional_embedding_visual = nn.Parameter(
                scale * torch.randn((image_resolution // vision_patch_size) ** 2 + 1, vision_width))
            self.proj_visual = nn.Parameter(scale * torch.randn(vision_width, embed_dim))
            self.conv1_visual = nn.Conv2d(in_channels=3, out_channels=vision_width, kernel_size=vision_patch_size,
                                          stride=vision_patch_size, bias=False)
            self.ln_pre_visual = LayerNorm(vision_width)
            self.ln_post_visual = LayerNorm(vision_width)
        else:
            pass

        if isinstance(vision_layers, (tuple, list)):
            vision_heads = vision_width * 32 // 64
            self.visual = ModifiedResNet(
                layers=vision_layers,
                output_dim=embed_dim,
                heads=vision_heads,
                input_resolution=image_resolution,
                width=vision_width
            )
        else:
            vision_heads = vision_width // 64
            if trainer == "PRO":
                vision_heads = vision_width // 64
                self.resblocks = nn.Sequential(
                    *[ResidualAttentionBlock_PRO(vision_width, vision_heads, transformer_width, transformer_heads, prompt_attn=None,
                                            attn_mask_visual=None, attn_mask_text=attn_mask_text, design_details=design_details, i=i)
                      for i in range(vision_layers)])

            else:
                self.visual = VisionTransformer(
                    input_resolution=image_resolution,
                    patch_size=vision_patch_size,
                    width=vision_width,
                    layers=vision_layers,
                    heads=vision_heads,
                    output_dim=embed_dim,
                    design_details=design_details
                )

        if design_details['trainer'] == 'IVLP':
            prompt_till_layer_text = design_details['language_depth']
            self.transformer = Transformer(
                width=transformer_width,
                layers=transformer_layers,
                heads=transformer_heads,
                attn_mask=self.build_attention_mask(),
                prompts_needed=prompt_till_layer_text,
                text_layer=True,
                design_details=design_details
            )

        self.vocab_size = vocab_size
        self.token_embedding = nn.Embedding(vocab_size, transformer_width)
        self.positional_embedding = nn.Parameter(torch.empty(self.context_length, transformer_width))
        self.ln_final = LayerNorm(transformer_width)

        self.text_projection = nn.Parameter(torch.empty(transformer_width, embed_dim))
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

        self.initialize_parameters(design_details)

    def initialize_parameters(self, design_details):
        nn.init.normal_(self.token_embedding.weight, std=0.02)
        nn.init.normal_(self.positional_embedding, std=0.01)
        if design_details['parameter_sharing'] == False:
            proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
            attn_std = self.transformer.width ** -0.5
            fc_std = (2 * self.transformer.width) ** -0.5
            for block in self.transformer.resblocks:
                nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
                nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
                nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
                nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)

            if self.text_projection is not None:
                nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)
        else:
            proj_std = (self.transformer_width ** -0.5) * ((2 * self.transformer_layers) ** -0.5)
            attn_std = self.transformer_width ** -0.5
            fc_std = (2 * self.transformer_width) ** -0.5
            for block in self.resblocks:
                nn.init.normal_(block.text_attn.attn.in_proj_weight, std=attn_std)
                nn.init.normal_(block.text_attn.attn.out_proj.weight, std=proj_std)
                nn.init.normal_(block.text_attn.mlp.c_fc.weight, std=fc_std)
                nn.init.normal_(block.text_attn.mlp.c_proj.weight, std=proj_std)

            if self.text_projection is not None:
                nn.init.normal_(self.text_projection, std=self.transformer_width ** -0.5)

    def build_attention_mask(self):
        mask = torch.empty(self.context_length, self.context_length)
        mask.fill_(float("-inf"))
        mask.triu_(1)
        return mask

    @property
    def dtype(self):
        if self.parameters_sharing == True:
            return self.conv1_visual.weight.dtype
        else:
            return self.visual.conv1.weight.dtype

    def encode_image(self, image):
        return self.visual(image.type(self.dtype))

    def encode_text(self, text):
        x = self.token_embedding(text).type(self.dtype)

        x = x + self.positional_embedding.type(self.dtype)
        x = x.permute(1, 0, 2)
        x = self.transformer(x)
        x = x.permute(1, 0, 2)
        x = self.ln_final(x).type(self.dtype)
        x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection

        return x

    def forward(self, image, text):
        image_features = self.encode_image(image)
        text_features = self.encode_text(text)

        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

        logit_scale = self.logit_scale.exp()
        logits_per_image = logit_scale * image_features @ text_features.t()
        logits_per_text = logit_scale * text_features @ image_features.t()

        return logits_per_image, logits_per_text


def convert_weights(model: nn.Module):
    """Convert applicable model parameters to fp16"""

    def _convert_weights_to_fp16(l):
        if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
            l.weight.data = l.weight.data.half()
            if l.bias is not None:
                l.bias.data = l.bias.data.half()

        if isinstance(l, nn.MultiheadAttention):
            for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
                tensor = getattr(l, attr)
                if tensor is not None:
                    tensor.data = tensor.data.half()

        for name in ["text_projection", "proj"]:
            if hasattr(l, name):
                attr = getattr(l, name)
                if attr is not None:
                    attr.data = attr.data.half()

    model.apply(_convert_weights_to_fp16)


def build_model(state_dict: dict, design_details):
    vit = "visual.proj" in state_dict

    if vit:
        vision_width = state_dict["visual.conv1.weight"].shape[0]
        vision_layers = len(
            [k for k in state_dict.keys() if k.startswith("visual.") and k.endswith(".attn.in_proj_weight")])
        vision_patch_size = state_dict["visual.conv1.weight"].shape[-1]
        grid_size = round((state_dict["visual.positional_embedding"].shape[0] - 1) ** 0.5)
        image_resolution = vision_patch_size * grid_size
    else:
        counts: list = [len(set(k.split(".")[2] for k in state_dict if k.startswith(f"visual.layer{b}"))) for b in
                        [1, 2, 3, 4]]
        vision_layers = tuple(counts)
        vision_width = state_dict["visual.layer1.0.conv1.weight"].shape[0]
        output_width = round((state_dict["visual.attnpool.positional_embedding"].shape[0] - 1) ** 0.5)
        vision_patch_size = None
        assert output_width ** 2 + 1 == state_dict["visual.attnpool.positional_embedding"].shape[0]
        image_resolution = output_width * 32

    embed_dim = state_dict["text_projection"].shape[1]
    context_length = state_dict["positional_embedding"].shape[0]
    vocab_size = state_dict["token_embedding.weight"].shape[0]
    transformer_width = state_dict["ln_final.weight"].shape[0]
    transformer_heads = transformer_width // 64
    transformer_layers = len(set(k.split(".")[2] for k in state_dict if k.startswith(f"transformer.resblocks")))

    model = CLIP(
        embed_dim,
        image_resolution, vision_layers, vision_width, vision_patch_size,
        context_length, vocab_size, transformer_width, transformer_heads, transformer_layers, design_details
    )

    for key in ["input_resolution", "context_length", "vocab_size"]:
        if key in state_dict:
            del state_dict[key]

    new_state_dict = dict()
    for k, v in state_dict.items():
        if 'visual.transformer.resblocks' in k:
            name = k.replace('visual.transformer.resblocks.', '')
            name = 'resblocks.'+name.split('.', 1)[0]+'.'+'visual_attn.'+name.split('.', 1)[1]
        elif 'visual.' in k:
            if 'weight' in k or 'bias' in k:
                name = k.replace('visual.', '')
                name = name.split('.', 1)[0] + '_visual.'+ name.split('.', 1)[1]
            else:
                name = k.replace('visual.', '')
                name = name + '_visual'
        elif 'transformer.resblocks' in k:
            name = k.replace('transformer.resblocks.', '')
            name = 'resblocks.'+name.split('.', 1)[0]+'.'+'text_attn.'+name.split('.', 1)[1]
        else:
            name = k
        new_state_dict[name] = v

    convert_weights(model)
    if design_details['trainer'] == 'PRO':
        try:
            model.load_state_dict(new_state_dict)
        except:
            missing_keys, _ = model.load_state_dict(new_state_dict, strict=False)
            print('Weights not found for some missing keys: ', missing_keys)
    else:
        try:
            model.load_state_dict(state_dict)
        except:
            missing_keys, _ = model.load_state_dict(state_dict, strict=False)
            print('Weights not found for some missing keys: ', missing_keys)
    return model.eval()
