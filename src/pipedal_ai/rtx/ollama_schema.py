from __future__ import annotations

from copy import deepcopy
import re

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


def control_schema(control: dict) -> dict:
    """Use catalogue symbols/types, not generic synthesizer parameter names."""
    datatype = control.get("datatype", "number")
    if datatype == "boolean":
        return {"type": "boolean"}
    result = {"type": "integer" if datatype in {"integer", "enumeration"} else "number"}
    if datatype == "enumeration" and control.get("scale_points"):
        result["enum"] = [point["value"] for point in control["scale_points"]]
    minimum, maximum = control.get("minimum"), control.get("maximum")
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    default = control.get("default")
    # Some LV2 controls use an out-of-range default as an explicit sentinel.
    # Preserve that exact exception, already accepted by the Pi validator.
    if default is not None and ((minimum is not None and default < minimum) or
                                (maximum is not None and default > maximum)):
        return {"anyOf": [result, {"type": result["type"], "enum": [default]}]}
    return result


def planning_schema(payload: dict) -> dict:
    from ..models import PlanDraft

    schema = generation_schema(PlanDraft)
    schema["properties"]["request_id"] = {"type": "string", "enum": [payload["request_id"]]}
    for key, value in payload["catalog"].items():
        schema["properties"]["catalog"]["properties"][key] = {
            "type": "integer" if key == "revision" else "string", "enum": [value],
        }
    shortlist = payload["candidate_shortlist"]
    variant = schema["properties"]["variants"]["items"]
    chain = variant["properties"]["chain"]
    chain["maxItems"] = min(chain["maxItems"], payload["tone_intent"]["chain_constraints"]["max_plugins"])
    base_step = chain["items"]
    definitions = {}
    branches = []
    for index, plugin in enumerate(shortlist["plugins"]):
        step = deepcopy(base_step)
        step["title"] = plugin["name"]
        properties = step["properties"]
        properties["plugin_id"] = {"type": "string", "enum": [plugin["plugin_id"]]}
        properties["parameters"] = {
            "type": "object", "additionalProperties": False,
            "properties": {control["symbol"]: control_schema(control) for control in plugin["controls"]},
            "maxProperties": 64,
        }
        resources = []
        unavailable = False
        for role in plugin["resource_roles"]:
            ids = [asset["asset_id"] for asset in shortlist["assets"] if asset["resource_role"] == role]
            if not ids:
                unavailable = True
                break
            resources.append({
                "type": "object", "additionalProperties": False,
                "required": ["role", "asset_id"],
                "properties": {"role": {"type": "string", "enum": [role]},
                               "asset_id": {"type": "string", "enum": ids}},
            })
        if unavailable:
            continue
        properties["resources"] = {"type": "array", "minItems": len(resources), "maxItems": len(resources)}
        if not resources:
            properties["resources"] = {"type": "array", "enum": [[]]}
        elif len(resources) == 1:
            properties["resources"]["items"] = resources[0]
        elif resources:
            properties["resources"]["prefixItems"] = resources
        if resources:
            step["required"].append("resources")
        # Shared references avoid triplicating all plugin constraints for variants.
        label = re.sub(r"[^a-z0-9]+", "_", plugin["name"].casefold()).strip("_")
        name = f"{label}_{index}"
        definitions[name] = step
        branches.append({"$ref": f"#/$defs/{name}"})
    if not branches:
        raise ValueError("Aucun plugin réalisable avec les ressources de la liste courte.")
    chain["items"] = {"anyOf": branches}
    definitions["chain"] = chain
    variant["properties"]["chain"] = {"$ref": "#/$defs/chain"}
    variants = []
    for name in ("conservative", "balanced", "bold"):
        item = deepcopy(variant)
        item["properties"]["variant"] = {"type": "string", "enum": [name]}
        variants.append(item)
    schema["properties"]["variants"] = {
        # maxItems closes the tuple. Omit items:false: some grammar converters
        # prioritize items over prefixItems and cannot visit a boolean schema.
        "type": "array", "minItems": 3, "maxItems": 3, "prefixItems": variants,
    }
    schema["$defs"] = definitions
    return schema
