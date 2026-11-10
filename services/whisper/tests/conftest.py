import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Тесты не должны скачать large-v3-turbo и не должны погасить процесс по простою.
os.environ["WHISPER_MODEL"] = "tiny"
os.environ["WHISPER_COMPUTE_TYPE"] = "int8"
os.environ["WHISPER_DEVICE"] = "cpu"
os.environ["WHISPER_CPU_THREADS"] = "2"
os.environ["WHISPER_IDLE_EXIT_SEC"] = "0"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.pop("WHISPER_TOKEN", None)
