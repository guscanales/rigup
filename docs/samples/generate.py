# Regenerates docs/samples with FLUX 3 Klein through the ComfyUI API (run on the GPU server while ComfyUI mode is up).
# 16 steps, CFG 1, no prompt upsampler: the prompts are already written in the upsampler's style.
import json, math, random, time, urllib.request
NOTEXT = " Absolutely no text, no typography, no letters, no logos, no watermark."
SHOTS = {
 "landscape": (2048, 1152, "Full-color outdoor photograph of the Patagonian Andes at golden hour. In the foreground, a glacial lake with still turquoise water mirrors the mountains; smooth grey boulders and tufts of golden grass line the near shore on the lower left. In the midground, forests of southern beech in deep red and orange autumn color climb the slopes. In the background, three jagged granite towers rise, their faces lit warm orange by a low sun from frame-right, a single lenticular cloud above the central tower against a pale blue sky. Long shadows stretch across the valley and a thin band of mist lies over the far end of the lake. Wide-angle landscape photograph, deep focus, every rock, ripple and leaf sharp, rich natural color, high dynamic range."),
 "portrait": (1536, 2048, "Close-up photograph, portrait of an elderly Quechua woman in her nineties with deep brown skin. Her face is covered in dense, deeply etched wrinkles, sun spots and fine creases around the mouth and eyes, the skin texture rendered pore by pore. Her eyes are strikingly clear pale grey-green with a dark limbal ring around each iris, looking straight into the camera, each eye holding a sharp window catchlight. She wears a dark red handwoven wool shawl with frayed edges over her shoulders and a black felt bowler hat; thin strands of white hair fall from under the hat across her forehead. Soft natural window light from frame-left, the right side of her face falling into gentle shadow. The background is a plain dark brown adobe wall, softly out of focus. 85mm portrait lens, shallow depth of field, individual eyelashes and the weave of the wool sharp."),
 "travel": (2048, 1360, "Full-color outdoor photograph of an early morning street in Kyoto's Higashiyama district. A stone-paved lane slopes gently uphill between two rows of traditional two-story wooden machiya houses with dark timber lattices and grey tiled roofs. In the background at the top of the lane, the five-story Yasaka pagoda rises against a soft pastel sky with low sun from frame-left catching its upper eaves. In the midground, a young woman traveler with a tan wool coat, a dark green backpack and a camera strap over her shoulder walks away from the camera up the slope. Unlit red paper lanterns with no writing hang from the eaves on the right. A few maple branches with orange leaves reach into the frame from the upper left, and the stones are still damp from overnight rain, reflecting the sky. Travel photograph, natural color, crisp detail."),
 "desk": (2048, 1152, "Full-color indoor photograph of a minimalist home office desk setup at night. On a solid walnut desk, a large curved ultrawide monitor in the center shows a dark-themed code editor with colored syntax highlighting, the lines too small and soft to read. In front of it, a mechanical keyboard with beige and grey keycaps and a matte black mouse rest on a dark grey felt desk mat. On the left, over-ear headphones hang on an aluminum stand next to a small brass desk lamp casting a warm pool of light; on the right, a potted snake plant and a white ceramic mug with a wisp of steam. Behind the desk, the wall is a vertical oak slat panel lit from below by a soft cyan LED strip along the back edge of the desk. A window on the far right shows a blurred city skyline at night with round bokeh lights. Sharp focus on the keyboard and desk surface, realistic reflections on the monitor bezel, clean natural shadows."),
}

def graph(prompt, w, h, seed, prefix):
    shift = math.log(16)   # BFL workflow default: alpha 16 -> log(alpha)
    return {
     "101": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux3-image-klein-int8-convrot.safetensors", "weight_dtype": "default"}},
     "102": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_4b_int8_convrot.safetensors", "type": "flux3", "device": "default"}},
     "103": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
     "104": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt + NOTEXT, "clip": ["102", 0]}},
     "105": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["102", 0]}},
     "106": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
     "132": {"class_type": "ModelSamplingFlux", "inputs": {"model": ["101", 0], "max_shift": shift, "base_shift": shift, "width": w, "height": h}},
     "107": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 16, "cfg": 1, "sampler_name": "euler", "scheduler": "simple", "denoise": 1,
              "model": ["132", 0], "positive": ["104", 0], "negative": ["105", 0], "latent_image": ["106", 0]}},
     "108": {"class_type": "VAEDecode", "inputs": {"samples": ["107", 0], "vae": ["103", 0]}},
     "9":   {"class_type": "SaveImage", "inputs": {"images": ["108", 0], "filename_prefix": prefix}},
    }

def post(path, body):
    return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8188" + path, json.dumps(body).encode(), {"Content-Type": "application/json"})))

if __name__ == "__main__":   # other scripts import SHOTS without re-running this
    ids = {}
    for name, (w, h, p) in SHOTS.items():
        for v in (1, 2):
            seed = random.randrange(2**48)
            r = post("/prompt", {"prompt": graph(p, w, h, seed, f"klein/{name}_{v}")})
            if r.get("node_errors"):
                raise SystemExit(json.dumps(r["node_errors"])[:2000])
            ids[r["prompt_id"]] = f"{name}_{v} seed={seed}"
    print("queued", len(ids), flush=True)
    t0 = time.time()
    while ids:
        for pid in list(ids):
            h = json.load(urllib.request.urlopen(f"http://127.0.0.1:8188/history/{pid}"))
            if pid in h:
                st = h[pid]["status"]
                files = [i["filename"] for o in h[pid]["outputs"].values() for i in o.get("images", [])]
                print(f"{time.time()-t0:5.0f}s {ids.pop(pid)} {st['status_str']} {files}", flush=True)
                if st["status_str"] != "success":
                    print(json.dumps(st["messages"])[-1500:])
        time.sleep(3)
