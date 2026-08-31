from jsonpath_ng.ext import parse


def extract_json_value(data: object, path: str) -> float:
    jsonpath_expr = parse(path)
    match = jsonpath_expr.find(data)
    if match:
        return float(match[0].value)
    else:
        raise ValueError("No match found for the JSON path")
