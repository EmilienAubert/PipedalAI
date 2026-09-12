from __future__ import annotations

from copy import deepcopy

from pydantic import BaseModel


def generation_schema(model_type: type[BaseModel]) -> dict:
    """Derive an inline schema from the validation model, without a second contract.

    These contracts have no recursive types. Inline references reduce grammar and
    prompt complexity; validation still uses the original strict Pydantic model.
    """
    schema = model_type.model_json_schema()
    definitions = schema.get("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        source = deepcopy(value)
        reference = source.pop("$ref", None)
        if reference:
            if not reference.startswith("#/$defs/"):
                raise ValueError(f"Unsupported schema reference: {reference}")
            source = {**deepcopy(definitions[reference.rsplit('/', 1)[-1]]), **source}
        result = {}
        for key, item in source.items():
            if key in {"$defs", "title", "description", "default", "examples"}:
                continue
            # Property names are data, not schema keywords (e.g. description).
            result[key] = ({name: expand(child) for name, child in item.items()}
                           if key == "properties" else expand(item))
        if "const" in result:
            result["enum"] = [result.pop("const")]
        return result

    return expand(schema)
