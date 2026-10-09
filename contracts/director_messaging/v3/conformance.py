from pathlib import Path
import json
from jsonschema import Draft202012Validator
VALIDATOR = Draft202012Validator(json.loads(Path(__file__).with_name("schema.json").read_text()))
def validate_envelope(value):
    VALIDATOR.validate(value)
