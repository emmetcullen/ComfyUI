import torch
import copy
import inspect

import comfy.utils
import comfy.model_management

class ModelPatcher:
    def __init__(self, model, load_device, offload_device, size=0, current_device=None, weight_inplace_update=False):
        self.size = size
        self.model = model
        self.patches = {}
        self.backup = {}
        self.object_patches = {}
        self.object_patches_backup = {}
        self.model_options = {"transformer_options":{}}
        self.model_size()
        self.load_device = load_device
        self.offload_device = offload_device
        if current_device is None:
            self.current_device = self.offload_device
        else:
            self.current_device = current_device

        self.weight_inplace_update = weight_inplace_update

    def model_size(self):
        if self.size > 0:
            return self.size
        model_sd = self.model.state_dict()
        size = 0
        for k in model_sd:
            t = model_sd[k]
            size += t.nelement() * t.element_size()
        self.size = size
        self.model_keys = set(model_sd.keys())
        return size

    def clone(self):
        n = ModelPatcher(self.model, self.load_device, self.offload_device, self.size, self.current_device)
        n.patches = {}
        for k in self.patches:
            n.patches[k] = self.patches[k][:]

        n.object_patches = self.object_patches.copy()
        n.model_options = copy.deepcopy(self.model_options)
        n.model_keys = self.model_keys
        return n

    def is_clone(self, other):
        if hasattr(other, 'model') and self.model is other.model:
            return True
        return False

    def set_model_sampler_cfg_function(self, sampler_cfg_function):
        if len(inspect.signature(sampler_cfg_function).parameters) == 3:
            self.model_options["sampler_cfg_function"] = lambda args: sampler_cfg_function(args["cond"], args["uncond"], args["cond_scale"]) #Old way
        else:
            self.model_options["sampler_cfg_function"] = sampler_cfg_function

    def set_model_unet_function_wrapper(self, unet_wrapper_function):
        self.model_options["model_function_wrapper"] = unet_wrapper_function

    def set_model_patch(self, patch, name):
        to = self.model_options["transformer_options"]
        if "patches" not in to:
            to["patches"] = {}
        to["patches"][name] = to["patches"].get(name, []) + [patch]

    def set_model_patch_replace(self, patch, name, block_name, number):
        to = self.model_options["transformer_options"]
        if "patches_replace" not in to:
            to["patches_replace"] = {}
        if name not in to["patches_replace"]:
            to["patches_replace"][name] = {}
        to["patches_replace"][name][(block_name, number)] = patch

    def set_model_attn1_patch(self, patch):
        self.set_model_patch(patch, "attn1_patch")

    def set_model_attn2_patch(self, patch):
        self.set_model_patch(patch, "attn2_patch")

    def set_model_attn1_replace(self, patch, block_name, number):
        self.set_model_patch_replace(patch, "attn1", block_name, number)

    def set_model_attn2_replace(self, patch, block_name, number):
        self.set_model_patch_replace(patch, "attn2", block_name, number)

    def set_model_attn1_output_patch(self, patch):
        self.set_model_patch(patch, "attn1_output_patch")

    def set_model_attn2_output_patch(self, patch):
        self.set_model_patch(patch, "attn2_output_patch")

    def set_model_output_block_patch(self, patch):
        self.set_model_patch(patch, "output_block_patch")

    def add_object_patch(self, name, obj):
        self.object_patches[name] = obj

    def model_patches_to(self, device):
        to = self.model_options["transformer_options"]
        if "patches" in to:
            patches = to["patches"]
            for name in patches:
                patch_list = patches[name]
                for i in range(len(patch_list)):
                    if hasattr(patch_list[i], "to"):
                        patch_list[i] = patch_list[i].to(device)
        if "patches_replace" in to:
            patches = to["patches_replace"]
            for name in patches:
                patch_list = patches[name]
                for k in patch_list:
                    if hasattr(patch_list[k], "to"):
                        patch_list[k] = patch_list[k].to(device)
        if "model_function_wrapper" in self.model_options:
            wrap_func = self.model_options["model_function_wrapper"]
            if hasattr(wrap_func, "to"):
                self.model_options["model_function_wrapper"] = wrap_func.to(device)

    def model_dtype(self):
        if hasattr(self.model, "get_dtype"):
            return self.model.get_dtype()

    def add_patches(self, patches, strength_patch=1.0, strength_model=1.0):
        p = set()
        for k in patches:
            if k in self.model_keys:
                p.add(k)
                current_patches = self.patches.get(k, [])
                current_patches.append((strength_patch, patches[k], strength_model))
                self.patches[k] = current_patches

        return list(p)

    def get_key_patches(self, filter_prefix=None):
        comfy.model_management.unload_model_clones(self)
        model_sd = self.model_state_dict()
        p = {}
        for k in model_sd:
            if filter_prefix is not None:
                if not k.startswith(filter_prefix):
                    continue
            if k in self.patches:
                p[k] = [model_sd[k]] + self.patches[k]
            else:
                p[k] = (model_sd[k],)
        return p

    def model_state_dict(self, filter_prefix=None):
        sd = self.model.state_dict()
        keys = list(sd.keys())
        if filter_prefix is not None:
            for k in keys:
                if not k.startswith(filter_prefix):
                    sd.pop(k)
        return sd

    def patch_model(self, device_to=None):
        for k in self.object_patches:
            old = getattr(self.model, k)
            if k not in self.object_patches_backup:
                self.object_patches_backup[k] = old
            setattr(self.model, k, self.object_patches[k])

        model_sd = self.model_state_dict()
        for key in self.patches:
            if key not in model_sd:
                print("could not patch. key doesn't exist in model:", key)
                continue

            weight = model_sd[key]

            inplace_update = self.weight_inplace_update

            if key not in self.backup:
                self.backup[key] = weight.to(device=self.offload_device, copy=inplace_update)

            if device_to is not None:
                temp_weight = comfy.model_management.cast_to_device(weight, device_to, torch.float32, copy=True)
            else:
                temp_weight = weight.to(torch.float32, copy=True)
            out_weight = self.calculate_weight(self.patches[key], temp_weight, key).to(weight.dtype)
            if inplace_update:
                comfy.utils.copy_to_param(self.model, key, out_weight)
            else:
                comfy.utils.set_attr(self.model, key, out_weight)
            del temp_weight

        if device_to is not None:
            self.model.to(device_to)
            self.current_device = device_to

        return self.model

    def calculate_weight(self, patches, weight, key):
        for p in patches:
            alpha = p[0]
            v = p[1]
            strength_model = p[2]

            if strength_model != 1.0:
                weight *= strength_model

            if isinstance(v, list):
                v = (self.calculate_weight(v[1:], v[0].clone(), key), )

            if len(v) == 1:
                w1 = v[0]
                if alpha != 0.0:
                    if w1.shape != weight.shape:
                        print("WARNING SHAPE MISMATCH {} WEIGHT NOT MERGED {} != {}".format(key, w1.shape, weight.shape))
                    else:
                        weight += alpha * comfy.model_management.cast_to_device(w1, weight.device, weight.dtype)
            elif len(v) == 4: #lora/locon
                mat1 = comfy.model_management.cast_to_device(v[0], weight.device, torch.float32)
                mat2 = comfy.model_management.cast_to_device(v[1], weight.device, torch.float32)
                if v[2] is not None:
                    alpha *= v[2] / mat2.shape[0]
                if v[3] is not None:
                    #locon mid weights, hopefully the math is fine because I didn't properly test it
                    mat3 = comfy.model_management.cast_to_device(v[3], weight.device, torch.float32)
                    final_shape = [mat2.shape[1], mat2.shape[0], mat3.shape[2], mat3.shape[3]]
                    mat2 = torch.mm(mat2.transpose(0, 1).flatten(start_dim=1), mat3.transpose(0, 1).flatten(start_dim=1)).reshape(final_shape).transpose(0, 1)
                try:
                    weight += (alpha * torch.mm(mat1.flatten(start_dim=1), mat2.flatten(start_dim=1))).reshape(weight.shape).type(weight.dtype)
                except Exception as e:
                    print("ERROR", key, e)
            elif len(v) == 8: #lokr
                w1 = v[0]
                w2 = v[1]
                w1_a = v[3]
                w1_b = v[4]
                w2_a = v[5]
                w2_b = v[6]
                t2 = v[7]
                dim = None

                if w1 is None:
                    dim = w1_b.shape[0]
                    w1 = torch.mm(comfy.model_management.cast_to_device(w1_a, weight.device, torch.float32),
                                  comfy.model_management.cast_to_device(w1_b, weight.device, torch.float32))
                else:
                    w1 = comfy.model_management.cast_to_device(w1, weight.device, torch.float32)

                if w2 is None:
                    dim = w2_b.shape[0]
                    if t2 is None:
                        w2 = torch.mm(comfy.model_management.cast_to_device(w2_a, weight.device, torch.float32),
                                      comfy.model_management.cast_to_device(w2_b, weight.device, torch.float32))
                    else:
                        w2 = torch.einsum('i j k l, j r, i p -> p r k l',
                                          comfy.model_management.cast_to_device(t2, weight.device, torch.float32),
                                          comfy.model_management.cast_to_device(w2_b, weight.device, torch.float32),
                                          comfy.model_management.cast_to_device(w2_a, weight.device, torch.float32))
                else:
                    w2 = comfy.model_management.cast_to_device(w2, weight.device, torch.float32)

                if len(w2.shape) == 4:
                    w1 = w1.unsqueeze(2).unsqueeze(2)
                if v[2] is not None and dim is not None:
                    alpha *= v[2] / dim

                try:
                    weight += alpha * torch.kron(w1, w2).reshape(weight.shape).type(weight.dtype)
                except Exception as e:
                    print("ERROR", key, e)
            else: #loha
                w1a = v[0]
                w1b = v[1]
                if v[2] is not None:
                    alpha *= v[2] / w1b.shape[0]
                w2a = v[3]
                w2b = v[4]
                if v[5] is not None: #cp decomposition
                    t1 = v[5]
                    t2 = v[6]
                    m1 = torch.einsum('i j k l, j r, i p -> p r k l',
                                      comfy.model_management.cast_to_device(t1, weight.device, torch.float32),
                                      comfy.model_management.cast_to_device(w1b, weight.device, torch.float32),
                                      comfy.model_management.cast_to_device(w1a, weight.device, torch.float32))

                    m2 = torch.einsum('i j k l, j r, i p -> p r k l',
                                      comfy.model_management.cast_to_device(t2, weight.device, torch.float32),
                                      comfy.model_management.cast_to_device(w2b, weight.device, torch.float32),
                                      comfy.model_management.cast_to_device(w2a, weight.device, torch.float32))
                else:
                    m1 = torch.mm(comfy.model_management.cast_to_device(w1a, weight.device, torch.float32),
                                  comfy.model_management.cast_to_device(w1b, weight.device, torch.float32))
                    m2 = torch.mm(comfy.model_management.cast_to_device(w2a, weight.device, torch.float32),
                                  comfy.model_management.cast_to_device(w2b, weight.device, torch.float32))

                try:
                    weight += (alpha * m1 * m2).reshape(weight.shape).type(weight.dtype)
                except Exception as e:
                    print("ERROR", key, e)

        return weight

    def unpatch_model(self, device_to=None):
        keys = list(self.backup.keys())

        if self.weight_inplace_update:
            for k in keys:
                comfy.utils.copy_to_param(self.model, k, self.backup[k])
        else:
            for k in keys:
                comfy.utils.set_attr(self.model, k, self.backup[k])

        self.backup = {}

        if device_to is not None:
            self.model.to(device_to)
            self.current_device = device_to

        keys = list(self.object_patches_backup.keys())
        for k in keys:
            setattr(self.model, k, self.object_patches_backup[k])

        self.object_patches_backup = {}
