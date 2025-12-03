import re,json, yaml

def parse_llm_response(response_text):
    """
    Robustly parse LLM response that might be JSON or YAML,
    possibly wrapped in markdown code fences or preceded by preamble text.

    Args:
        response_text: Raw text from LLM

    Returns:
        dict: Parsed content

    Raises:
        ValueError: If parsing fails after all attempts
    """
    if not response_text or not response_text.strip():
        raise ValueError("Empty response from LLM")

    cleaned = response_text.strip()

    # Strategy 1: Extract content between code fences
    code_fence_pattern = r"```(?:json|yaml|yml)?\s*\n(.*?)\n```"
    code_fence_match = re.search(code_fence_pattern, cleaned, re.DOTALL)
    if code_fence_match:
        cleaned = code_fence_match.group(1).strip()
    else:
        # Strategy 2: Remove leading markdown code fence markers
        cleaned = re.sub(
            r"^```(?:json|yaml|yml)?\s*\n", "", cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(r"\n```\s*$", "", cleaned, flags=re.MULTILINE)

    # Strategy 3: Try to find JSON object or YAML content after preamble
    # Look for content starting with { or a YAML key pattern
    json_match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    yaml_match = re.search(
        r"^([a-zA-Z_][\w]*\s*:.*)", cleaned, re.DOTALL | re.MULTILINE
    )

    # Prepare multiple candidates to try parsing
    candidates = [cleaned]

    if json_match:
        candidates.insert(0, json_match.group(1).strip())

    if yaml_match:
        candidates.insert(0, yaml_match.group(1).strip())

    # Try parsing each candidate
    errors = []

    for candidate in candidates:
        if not candidate:
            continue

        # Try JSON first (faster and more strict)
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as e:
            errors.append(f"JSON parse error: {e}")

        # Try YAML (more forgiving)
        try:
            result = yaml.safe_load(candidate)
            if result is None:
                continue
            if isinstance(result, dict):
                return result
        except yaml.YAMLError as e:
            errors.append(f"YAML parse error: {e}")

    # If all attempts failed, raise detailed error
    error_msg = f"Failed to parse LLM response after trying all strategies.\n"
    error_msg += f"Errors encountered:\n" + "\n".join(f"  - {e}" for e in errors)
    error_msg += f"\n\nRaw output (first 500 chars):\n{response_text[:500]}"
    raise ValueError(error_msg)
