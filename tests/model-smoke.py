"""Load the shipped model and transcribe one second of generated silence offline."""
import hashlib
import json
import os
import sys
import time
import wave
from pathlib import Path

root = Path(__file__).resolve().parent.parent
resources = Path(os.environ.get('TEST_RESOURCES', str(root)))
os.environ["PATH"] = str(resources / "runtime" / "ffmpeg") + os.pathsep + os.environ["PATH"]
import whisper
import torch

model_file = resources / "runtime" / "models" / "medium.pt"
started = time.time()
expected = whisper._MODELS["medium"].split("/")[-2]
digest = hashlib.file_digest(model_file.open("rb"), "sha256").hexdigest()
assert digest == expected, "Whisper model checksum mismatch"
artifact = root / "tests" / "artifacts" / "model"
artifact.mkdir(parents=True, exist_ok=True)
wav = artifact / "silence.wav"
with wave.open(str(wav), "wb") as out:
    out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    out.writeframes(b"\0\0" * 16000)
torch.set_num_threads(4)
model = whisper.load_model(str(model_file), device="cpu")
result = model.transcribe(str(wav), language="en", fp16=False, temperature=0, condition_on_previous_text=False)
report = {"ok": True, "model_sha256": digest, "model_loaded": True, "ffmpeg_decode": True, "transcription_completed": True, "elapsed_s": round(time.time() - started, 2), "text": result["text"]}
(artifact / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
