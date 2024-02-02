import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from PIL import Image

import modules.config
from modules.flags import MetadataScheme, Performance, Steps
from modules.util import quote, unquote, extract_styles_from_prompt, is_json, calculate_sha256

re_param_code = r'\s*(\w[\w \-/]+):\s*("(?:\\.|[^\\"])+"|[^,]*)(?:,|$)'
re_param = re.compile(re_param_code)
re_imagesize = re.compile(r"^(\d+)x(\d+)$")

hash_cache = {}


def get_sha256(filepath):
    global hash_cache

    if filepath not in hash_cache:
        hash_cache[filepath] = calculate_sha256(filepath)

    return hash_cache[filepath]


class MetadataParser(ABC):
    def __init__(self):
        self.full_prompt: str = ''
        self.full_negative_prompt: str = ''
        self.steps: int = 30
        self.base_model_name: str = ''
        self.base_model_hash: str = ''
        self.refiner_model_name: str = ''
        self.refiner_model_hash: str = ''
        self.loras: list = []

    @abstractmethod
    def get_scheme(self) -> MetadataScheme:
        raise NotImplementedError

    @abstractmethod
    def parse_json(self, metadata: dict) -> dict:
        raise NotImplementedError

    @abstractmethod
    def parse_string(self, metadata: dict) -> str:
        raise NotImplementedError

    def set_data(self, full_prompt, full_negative_prompt, steps, base_model_name, refiner_model_name, loras):
        self.full_prompt = full_prompt
        self.full_negative_prompt = full_negative_prompt
        self.steps = steps
        self.base_model_name = Path(base_model_name).stem

        base_model_path = os.path.join(modules.config.path_checkpoints, base_model_name)
        self.base_model_hash = get_sha256(base_model_path)

        if refiner_model_name not in ['', 'None']:
            self.refiner_model_name = Path(refiner_model_name).stem
            refiner_model_path = os.path.join(modules.config.path_checkpoints, refiner_model_name)
            self.refiner_model_hash = get_sha256(refiner_model_path)

        self.loras = []
        for (lora_name, lora_weight) in loras:
            if lora_name != 'None':
                lora_path = os.path.join(modules.config.path_loras, lora_name)
                lora_hash = get_sha256(lora_path)
                self.loras.append((Path(lora_name).stem, lora_weight, lora_hash))


class A1111MetadataParser(MetadataParser):
    def get_scheme(self) -> MetadataScheme:
        return MetadataScheme.A1111

    fooocus_to_a1111 = {
        'negative_prompt': 'Negative prompt',
        'styles': 'Styles',
        'performance': 'Performance',
        'steps': 'Steps',
        'sampler': 'Sampler',
        'guidance_scale': 'CFG scale',
        'seed': 'Seed',
        'resolution': 'Size',
        'sharpness': 'Sharpness',
        'adm_guidance': 'ADM Guidance',
        'refiner_swap_method': 'Refiner Swap Method',
        'adaptive_cfg': 'Adaptive CFG',
        'overwrite_switch': 'Overwrite Switch',
        'freeu': 'FreeU',
        'base_model': 'Model',
        'base_model_hash': 'Model hash',
        'refiner_model': 'Refiner',
        'refiner_model_hash': 'Refiner hash',
        'lora_hashes': 'Lora hashes',
        'lora_weights': 'Lora weights',
        'created_by': 'User',
        'version': 'Version'
    }

    def parse_json(self, metadata: str) -> dict:
        prompt = ''
        negative_prompt = ''

        done_with_prompt = False

        *lines, lastline = metadata.strip().split("\n")
        if len(re_param.findall(lastline)) < 3:
            lines.append(lastline)
            lastline = ''

        for line in lines:
            line = line.strip()
            if line.startswith(f"{self.fooocus_to_a1111['negative_prompt']}:"):
                done_with_prompt = True
                line = line[len(f"{self.fooocus_to_a1111['negative_prompt']}:"):].strip()
            if done_with_prompt:
                negative_prompt += ('' if negative_prompt == '' else "\n") + line
            else:
                prompt += ('' if prompt == '' else "\n") + line

        found_styles, prompt, negative_prompt = extract_styles_from_prompt(prompt, negative_prompt)

        data = {
            'prompt': prompt,
            'negative_prompt': negative_prompt,
            'styles': str(found_styles)
        }

        for k, v in re_param.findall(lastline):
            try:
                if v[0] == '"' and v[-1] == '"':
                    v = unquote(v)

                m = re_imagesize.match(v)
                if m is not None:
                    data[f'resolution'] = str((m.group(1), m.group(2)))
                else:
                    data[list(self.fooocus_to_a1111.keys())[list(self.fooocus_to_a1111.values()).index(k)]] = v
            except Exception:
                print(f"Error parsing \"{k}: {v}\"")

        # try to load performance based on steps, fallback for direct A1111 imports
        if 'steps' in data and 'performance' not in data:
            try:
                data['performance'] = Performance[Steps(int(data['steps'])).name].value
            except Exception:
                pass

        if 'base_model' in data:
            for filename in modules.config.model_filenames:
                path = Path(filename)
                if data['base_model'] == path.stem:
                    data['base_model'] = filename
                    break

        if 'lora_hashes' in data:
            # TODO optimize by using hash for matching. Problem is speed of creating the hash per model, even on startup
            lora_filenames = modules.config.lora_filenames.copy()
            lora_filenames.remove(modules.config.downloading_sdxl_lcm_lora())
            for li, lora in enumerate(data['lora_hashes'].split(', ')):
                lora_name, lora_hash, lora_weight = lora.split(': ')
                for filename in lora_filenames:
                    path = Path(filename)
                    if lora_name == path.stem:
                        data[f'lora_combined_{li + 1}'] = f'{filename} : {lora_weight}'
                        break

        return data

    def parse_string(self, metadata: dict) -> str:
        data = {k: v for _, k, v in metadata}

        width, height = eval(data['resolution'])

        generation_params = {
            self.fooocus_to_a1111['performance']: data['performance'],
            self.fooocus_to_a1111['steps']: self.steps,
            self.fooocus_to_a1111['sampler']: data['sampler'],
            self.fooocus_to_a1111['seed']: data['seed'],
            self.fooocus_to_a1111['resolution']: f'{width}x{height}',
            self.fooocus_to_a1111['guidance_scale']: data['guidance_scale'],
            self.fooocus_to_a1111['sharpness']: data['sharpness'],
            self.fooocus_to_a1111['adm_guidance']: data['adm_guidance'],
            self.fooocus_to_a1111['base_model']: Path(data['base_model']).stem,
            self.fooocus_to_a1111['base_model_hash']: self.base_model_hash,
        }

        # TODO evaluate if this should always be added
        if self.refiner_model_name not in ['', 'None']:
            generation_params |= {
                self.fooocus_to_a1111['refiner_model']: self.refiner_model_name,
                self.fooocus_to_a1111['refiner_model_hash']: self.refiner_model_hash
            }

        for key in ['adaptive_cfg', 'overwrite_switch', 'refiner_swap_method', 'freeu']:
            if key in data:
                generation_params[self.fooocus_to_a1111[key]] = data[key]

        lora_hashes = []
        for index, (lora_name, lora_weight, lora_hash) in enumerate(self.loras):
            # workaround for Fooocus not knowing LoRA name in LoRA metadata
            lora_hashes.append(f'{lora_name}: {lora_hash}: {lora_weight}')
        lora_hashes_string = ', '.join(lora_hashes)

        generation_params |= {
            self.fooocus_to_a1111['lora_hashes']: lora_hashes_string,
            self.fooocus_to_a1111['version']: data['version']
        }

        if modules.config.metadata_created_by != '':
            generation_params[self.fooocus_to_a1111['created_by']] = modules.config.metadata_created_by

        generation_params_text = ", ".join(
            [k if k == v else f'{k}: {quote(v)}' for k, v in dict(sorted(generation_params.items())).items() if v is not None])
        # TODO check if multiline positive prompt is correctly processed
        positive_prompt_resolved = ', '.join(self.full_prompt)  # TODO add loras to positive prompt if even possible
        negative_prompt_resolved = ', '.join(
            self.full_negative_prompt)  # TODO add loras to negative prompt if even possible
        negative_prompt_text = f"\nNegative prompt: {negative_prompt_resolved}" if negative_prompt_resolved else ""
        return f"{positive_prompt_resolved}{negative_prompt_text}\n{generation_params_text}".strip()


class FooocusMetadataParser(MetadataParser):
    def get_scheme(self) -> MetadataScheme:
        return MetadataScheme.FOOOCUS

    def parse_json(self, metadata: dict) -> dict:
        model_filenames = modules.config.model_filenames.copy()
        lora_filenames = modules.config.lora_filenames.copy()
        lora_filenames.remove(modules.config.downloading_sdxl_lcm_lora())

        for key, value in metadata.items():
            if value in ['', 'None']:
                continue
            if key in ['base_model', 'refiner_model']:
                metadata[key] = self.replace_value_with_filename(key, value, model_filenames)
            elif key.startswith('lora_combined_'):
                metadata[key] = self.replace_value_with_filename(key, value, lora_filenames)
            else:
                continue

        return metadata

    def parse_string(self, metadata: list) -> str:
        for li, (label, key, value) in enumerate(metadata):
            # remove model folder paths from metadata
            if key.startswith('lora_combined_'):
                name, weight = value.split(' : ')
                name = Path(name).stem
                value = f'{name} : {weight}'
                metadata[li] = (label, key, value)

        res = {k: v for _, k, v in metadata}

        res['full_prompt'] = self.full_prompt
        res['full_negative_prompt'] = self.full_negative_prompt
        res['steps'] = self.steps
        res['base_model'] = self.base_model_name
        res['base_model_hash'] = self.base_model_hash

        # TODO evaluate if this should always be added
        if self.refiner_model_name not in ['', 'None']:
            res['refiner_model'] = self.refiner_model_name
            res['refiner_model_hash'] = self.refiner_model_hash

        res['loras'] = self.loras

        if modules.config.metadata_created_by != '':
            res['created_by'] = modules.config.metadata_created_by

        return json.dumps(dict(sorted(res.items())))

    @staticmethod
    def replace_value_with_filename(key, value, filenames):
        for filename in filenames:
            path = Path(filename)
            if key.startswith('lora_combined_'):
                name, weight = value.split(' : ')
                if name == path.stem:
                    return f'{filename} : {weight}'
            elif value == path.stem:
                return filename


def get_metadata_parser(metadata_scheme: MetadataScheme) -> MetadataParser:
    match metadata_scheme:
        case MetadataScheme.FOOOCUS:
            return FooocusMetadataParser()
        case MetadataScheme.A1111:
            return A1111MetadataParser()
        case _:
            raise NotImplementedError


def read_info_from_image(filepath) -> tuple[str | None, dict, MetadataScheme | None]:
    with Image.open(filepath) as image:
        items = (image.info or {}).copy()

    parameters = items.pop('parameters', None)
    if parameters is not None and is_json(parameters):
        parameters = json.loads(parameters)

    try:
        metadata_scheme = MetadataScheme(items.pop('fooocus_scheme', None))
    except Exception:
        metadata_scheme = None

    # broad fallback
    if metadata_scheme is None and isinstance(parameters, dict):
        metadata_scheme = modules.metadata.MetadataScheme.FOOOCUS

    if metadata_scheme is None and isinstance(parameters, str):
        metadata_scheme = modules.metadata.MetadataScheme.A1111

    return parameters, items, metadata_scheme
