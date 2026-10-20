# Same prompts as generate.py, rendered with Z-Image Turbo and Krea 2 Turbo (prompt expander and style LoRA off).
import json, random, sys, time, urllib.request
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from generate import SHOTS, NOTEXT, post   # same prompts and sizes as the FLUX 3 Klein run

def zimage(prompt, w, h, seed, prefix):
    return {
     "28": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
     "30": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
     "29": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
     "27": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt + NOTEXT, "clip": ["30", 0]}},
     "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["27", 0]}},
     "13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
     "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["28", 0], "shift": 3}},
     "3":  {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1,
            "model": ["11", 0], "positive": ["27", 0], "negative": ["33", 0], "latent_image": ["13", 0]}},
     "8":  {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["29", 0]}},
     "9":  {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }

def krea(prompt, w, h, seed, prefix):
    return {
     "10": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2_turbo_int8_convrot.safetensors", "weight_dtype": "default"}},
     "11": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2", "device": "default"}},
     "12": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
     "6":  {"class_type": "CLIPTextEncode", "inputs": {"text": prompt + NOTEXT, "clip": ["11", 0]}},
     "13": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
     "5":  {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
     "3":  {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 8, "cfg": 1, "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
            "model": ["10", 0], "positive": ["6", 0], "negative": ["13", 0], "latent_image": ["5", 0]}},
     "8":  {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["12", 0]}},
     "29": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }

ids = {}
for model, build in (("zimage", zimage), ("krea", krea)):
    for name, (w, h, p) in SHOTS.items():
        for v in (1, 2):
            seed = random.randrange(2**48)
            r = post("/prompt", {"prompt": build(p, w, h, seed, f"{model}/{name}_{v}")})
            if r.get("node_errors"):
                raise SystemExit(json.dumps(r["node_errors"])[:2000])
            ids[r["prompt_id"]] = f"{model}/{name}_{v} seed={seed}"
print("queued", len(ids), flush=True)
t0 = time.time()
while ids:
    for pid in list(ids):
        h = json.load(urllib.request.urlopen(f"http://127.0.0.1:8188/history/{pid}"))
        if pid in h:
            st = h[pid]["status"]
            print(f"{time.time()-t0:5.0f}s {ids.pop(pid)} {st['status_str']}", flush=True)
            if st["status_str"] != "success":
                print(json.dumps(st["messages"])[-1500:])
    time.sleep(3)
