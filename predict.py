from cog import BasePredictor, Input, Path
import os
import re
import time
import torch
import subprocess
import numpy as np
from PIL import Image
from typing import List
from diffusers import (
    FluxPipeline,
    FluxImg2ImgPipeline
)
from torchvision import transforms
from transformers import CLIPImageProcessor
from lora_loading_patch import load_lora_into_transformer
from diffusers.pipelines.stable_diffusion.safety_checker import (
    StableDiffusionSafetyChecker
)
import cv2  # Import OpenCV for FSRCNN

MAX_IMAGE_SIZE = 1440
MODEL_CACHE = "FLUX.1-schnell"
SAFETY_CACHE = "safety-cache"
FEATURE_EXTRACTOR = "/src/feature-extractor"
SAFETY_URL = "https://weights.replicate.delivery/default/sdxl/safety-1.0.tar"
MODEL_URL = "https://weights.replicate.delivery/default/black-forest-labs/FLUX.1-schnell/files.tar"

ASPECT_RATIOS = {
    "1:1": (1024, 1024),
    "16:9": (1344, 768),
    "21:9": (1536, 640),
    "3:2": (1216, 832),
    "2:3": (832, 1216),
    "4:5": (896, 1088),
    "5:4": (1088, 896),
    "3:4": (896, 1152),
    "4:3": (1152, 896),
    "9:16": (768, 1344),
    "9:21": (640, 1536),
}

def download_weights(url, dest, file=False):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    if not file:
        subprocess.check_call(["pget", "-xf", url, dest], close_fds=False)
    else:
        subprocess.check_call(["pget", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)

class Predictor(BasePredictor):
    def setup(self) -> None:
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()

        self.last_loaded_loras = {}

        print("Loading safety checker...")
        if not os.path.exists(SAFETY_CACHE):
            download_weights(SAFETY_URL, SAFETY_CACHE)
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(
            SAFETY_CACHE, torch_dtype=torch.float16
        ).to("cuda")
        self.feature_extractor = CLIPImageProcessor.from_pretrained(FEATURE_EXTRACTOR)

        print("Loading Flux txt2img Pipeline")
        if not os.path.exists(MODEL_CACHE):
            download_weights(MODEL_URL, '.')
        self.txt2img_pipe = FluxPipeline.from_pretrained(
            MODEL_CACHE,
            torch_dtype=torch.bfloat16,
            cache_dir=MODEL_CACHE
        ).to("cuda")
        self.txt2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )

        print("Loading Flux img2img pipeline")
        self.img2img_pipe = FluxImg2ImgPipeline(
            transformer=self.txt2img_pipe.transformer,
            scheduler=self.txt2img_pipe.scheduler,
            vae=self.txt2img_pipe.vae,
            text_encoder=self.txt2img_pipe.text_encoder,
            text_encoder_2=self.txt2img_pipe.text_encoder_2,
            tokenizer=self.txt2img_pipe.tokenizer,
            tokenizer_2=self.txt2img_pipe.tokenizer_2,
        ).to("cuda")
        self.img2img_pipe.__class__.load_lora_into_transformer = classmethod(
            load_lora_into_transformer
        )

        # Setup FSRCNN upscaler
        print("Loading FSRCNN upscaler...")
        self.upscaler = cv2.dnn_superres.DnnSuperResImpl_create()
        fsrcnn_weights = "FSRCNN_x4.pb"
        if not os.path.exists(fsrcnn_weights):
            raise FileNotFoundError(f"FSRCNN weights not found: {fsrcnn_weights}")
        self.upscaler.readModel(fsrcnn_weights)
        self.upscaler.setModel("fsrcnn", 4)

        print("setup took: ", time.time() - start)

    @torch.amp.autocast('cuda')
    def run_safety_checker(self, image):
        safety_checker_input = self.feature_extractor(image, return_tensors="pt").to("cuda")
        np_image = [np.array(val) for val in image]
        image, has_nsfw_concept = self.safety_checker(
            images=np_image,
            clip_input=safety_checker_input.pixel_values.to(torch.float16),
        )
        return image, has_nsfw_concept

    def aspect_ratio_to_width_height(self, aspect_ratio: str) -> tuple[int, int]:
        return ASPECT_RATIOS[aspect_ratio]

    def get_image(self, image: str):
        image = Image.open(image).convert("RGB")
        transform = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Lambda(lambda x: 2.0 * x - 1.0),
            ]
        )
        img: torch.Tensor = transform(image)
        return img[None, ...]

    @staticmethod
    def make_multiple_of_16(n):
        return ((n + 15) // 16) * 16

    def load_loras(self, lora_names, lora_scales):
        names = [
            'a','b','c','d','e','f','g','h','i','j','k','l','m',
            'n','o','p','q','r','s','t','u','v','w','x','y','z'
        ]
        count = 0

        for lora_filename in lora_names:
            local_path = os.path.join("loras", lora_filename)
            if not os.path.exists(local_path):
                raise FileNotFoundError(f"Could not find LoRA file at: {local_path}")
            adapter_name = names[count]
            count += 1
            print(f"Loading LoRA from local file: {local_path}")
            self.txt2img_pipe.load_lora_weights(local_path, adapter_name=adapter_name)

        adapter_names = names[:count]
        adapter_weights = lora_scales[:count]
        self.last_loaded_loras = lora_names
        self.txt2img_pipe.set_adapters(adapter_names, adapter_weights=adapter_weights)

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(description="Prompt for generated image"),
        aspect_ratio: str = Input(
            description="Aspect ratio for the generated image",
            choices=list(ASPECT_RATIOS.keys()),
            default="1:1"
        ),
        image: Path = Input(
            description="Input image for image to image mode. The aspect ratio of your output will match this image",
            default=None,
        ),
        prompt_strength: float = Input(
            description="Prompt strength (or denoising strength) when using image to image. 1.0 corresponds to full destruction of information in the image.",
            ge=0, le=1, default=0.5,
        ),
        num_outputs: int = Input(
            description="Number of images to output.",
            ge=1,
            le=4,
            default=1,
        ),
        num_inference_steps: int = Input(
            description="Number of inference steps",
            ge=1,
            le=50,
            default=4,
        ),
        guidance_scale: float = Input(
            description="Guidance scale for the diffusion process",
            ge=0,
            le=10,
            default=5.0,
        ),
        seed: int = Input(description="Random seed. Set for reproducible generation", default=None),
        output_format: str = Input(
            description="Format of the output images",
            choices=["webp", "jpg", "png"],
            default="webp",
        ),
        output_quality: int = Input(
            description="Quality when saving the output images, from 0 to 100. 100 is best quality, 0 is lowest quality. Not relevant for .png outputs",
            default=80,
            ge=0,
            le=100,
        ),
        hf_loras: list[str] = Input(
            description="List of file names in the 'loras' folder. Defaults to Cyberpunk Anime.",
            default=["Cyberpunk Anime.safetensors"],
        ),
        lora_scales: list[float] = Input(
            description="Scale for the LoRA weights. Default value is 0.8 if nothing is provided.",
            default=None,
        ),
        disable_safety_checker: bool = Input(
            description="Disable safety checker for generated images. This feature is only available through the API.",
            default=False,
        ),
        target_width: int = Input(
            description="Desired width for the upscaled image",
            default=2048
        ),
        target_height: int = Input(
            description="Desired height for the upscaled image",
            default=2048
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        width, height = self.aspect_ratio_to_width_height(aspect_ratio)
        max_sequence_length = 512

        flux_kwargs = {"width": width, "height": height}
        print(f"Prompt: {prompt}")
        device = self.txt2img_pipe.device

        if image:
            pipe = self.img2img_pipe
            print("img2img mode")
            init_image = self.get_image(image)
            width = init_image.shape[-1]
            height = init_image.shape[-2]
            print(f"Input image size: {width}x{height}")
            scale = min(MAX_IMAGE_SIZE / width, MAX_IMAGE_SIZE / height, 1)
            if scale < 1:
                width = int(width * scale)
                height = int(height * scale)
                print(f"Scaling image down to {width}x{height}")
            width = self.make_multiple_of_16(width)
            height = self.make_multiple_of_16(height)
            print(f"Input image size set to: {width}x{height}")
            init_image = init_image.to(device)
            init_image = torch.nn.functional.interpolate(init_image, (height, width))
            init_image = init_image.to(torch.bfloat16)
            flux_kwargs["image"] = init_image
            flux_kwargs["strength"] = prompt_strength
        else:
            print("txt2img mode")
            pipe = self.txt2img_pipe

        if hf_loras:
            flux_kwargs["joint_attention_kwargs"] = {"scale": 1.0}
            if hf_loras != self.last_loaded_loras:
                pipe.unload_lora_weights()
                if not lora_scales:
                    lora_scales = [0.8] * len(hf_loras)
                elif len(lora_scales) == 1 and len(hf_loras) > 1:
                    lora_scales = [lora_scales[0]] * len(hf_loras)
                self.load_loras(hf_loras, lora_scales)
        else:
            flux_kwargs["joint_attention_kwargs"] = None
            pipe.unload_lora_weights()

        pipe = pipe.to("cuda")

        generator = torch.Generator("cuda").manual_seed(seed)

        common_args = {
            "prompt": [prompt] * num_outputs,
            "guidance_scale": guidance_scale,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
            "max_sequence_length": max_sequence_length,
            "output_type": "pil"
        }

        output = pipe(**common_args, **flux_kwargs)

        if not disable_safety_checker:
            _, has_nsfw_content = self.run_safety_checker(output.images)

        output_paths = []
        for i, img in enumerate(output.images):
            if not disable_safety_checker and has_nsfw_content[i]:
                print(f"NSFW content detected in image {i}")
                continue
            output_path = f"/tmp/out-{i}.{output_format}"
            if output_format != 'png':
                img.save(output_path, quality=output_quality, optimize=True)
            else:
                img.save(output_path)
            output_paths.append(Path(output_path))

        if len(output_paths) == 0:
            raise Exception("NSFW content detected. Try running it again, or try a different prompt.")

        # Upscale images to target dimensions using FSRCNN
        upscaled_paths = []
        for path in output_paths:
            img = Image.open(path).convert("RGB")
            np_img = np.array(img)
            upscaled_img = self.upscaler.upsample(np_img)
            # Resize to the exact target dimensions
            upscaled_img = Image.fromarray(upscaled_img).resize((target_width, target_height), Image.LANCZOS)
            upscaled_path = str(path).replace(".", f"-upscaled.")
            if output_format != 'png':
                upscaled_img.save(upscaled_path, quality=output_quality, optimize=True)
            else:
                upscaled_img.save(upscaled_path)
            upscaled_paths.append(Path(upscaled_path))

        return upscaled_paths
