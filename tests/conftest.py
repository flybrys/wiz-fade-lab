import importlib.util
import sys
from pathlib import Path

path = Path(__file__).parents[1] / "custom_components/wiz_fade/engine.py"
spec = importlib.util.spec_from_file_location("wiz_fade_engine", path)
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)
