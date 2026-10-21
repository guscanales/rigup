"""Self-check for the parts of server.py that don't need a GPU or network: python3 test_server.py"""
import json, os, tempfile

os.environ["RIGUP_DATA"] = tempfile.mkdtemp()
import server as s

# engine flag helpers
a = ["--max-model-len", "81920", "--kv-cache-dtype", "fp8"]
assert s.get_flag(a, "--max-model-len") == "81920" and s.get_flag(a, "-c") is None
assert s.set_flag(a, "--max-model-len", 40960)[1] == "40960" and a[1] == "81920"   # returns a copy
assert s.set_flag(a, "-c", 8192)[-2:] == ["-c", "8192"]

# what to retry with after a failed start
vllm_err = "ValueError: ... Based on the available memory, the estimated maximum model length is 50000. Try increasing"
assert s.next_ctx(vllm_err, 81920) == 50000 * 95 // 100 // 1024 * 1024
assert s.next_ctx("CUDA error: out of memory", 65536) == 32768
assert s.next_ctx("ggml_backend_cuda_buffer_type_alloc_buffer: failed to allocate", 32768) == 16384
assert s.next_ctx("some unrelated crash", 32768) is None

# catalog merge: new upstream models are added and marked, existing ones are never overridden
first = next(iter(s.MODELS))
key = next(iter(s.GROUPS))   # "llm" on Linux, "mac" on macOS
up = {"images": {"vllm": "vllm/vllm-openai:v9.9.9"},
      key: [{"id": first, "name": "CHANGED"}, {"id": "brand-new", "name": "Brand New", "engine": "vllm", "fit": {"24": "estimate"}, "files": [{"repo": "x/y"}], "args": []}]}
s.write_json(s.CATALOG, up)
s.load_catalog()
assert s.MODELS[first]["name"] != "CHANGED" and not s.MODELS[first].get("new")
assert s.MODELS["brand-new"]["new"] and s.MODELS["brand-new"].get("image") == (None if s.MAC else "vllm/vllm-openai:v9.9.9")
assert s.IMAGES["vllm"] != "vllm/vllm-openai:v9.9.9"

# tuned args apply only on the GPU they were measured on
s.gpu = lambda: {"name": "Test GPU", "mem_total": 24576, "mem_used": 0, "ram_gb": 36}
assert s.tier_of(s.gpu()) == (32 if s.MAC else 24)   # a 36 GB Mac uses the 32 GB recipes
m = s.MODELS[first]
s.write_json(s.TUNED, {first: {"gpu": "Other GPU", "args": ["--tuned"]}})
assert s.args_for(m) == m["args"]
s.write_json(s.TUNED, {first: {"gpu": "Test GPU", "args": ["--tuned"]}})
assert s.args_for(m) == ["--tuned"]
s.write_json(s.TUNED, {first: {"gpu": "Test GPU", "error": "doesn't fit"}})
assert s.args_for(m) == m["args"]

print("ok")
