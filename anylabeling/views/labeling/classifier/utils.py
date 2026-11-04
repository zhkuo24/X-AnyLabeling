import os
import shutil
from typing import List, Dict

from anylabeling.views.labeling.utils.general import (
    resolve_export_directory,
    resolve_path_within_directory,
)


def _label_file():
    # Imported lazily: this module is loaded while LabelFile is still
    # initializing, via the labeling widget package.
    from anylabeling.views.labeling.label_file import LabelFile, LabelFileError

    return LabelFile, LabelFileError


def get_label_file_path(image_path: str, output_dir: str = None) -> str:
    label_file, _error = _label_file()
    return label_file.path_for_image(image_path, output_dir)


def load_flags_from_json(json_path: str) -> Dict[str, bool]:
    if not os.path.exists(json_path):
        return {}

    label_file, label_error = _label_file()
    try:
        data = label_file.read_document(json_path)
        return data.get("flags", {})
    except (label_error, KeyError, OSError):
        return {}


def save_flags_to_json(json_path: str, flags: Dict[str, bool]):
    label_file, _error = _label_file()
    data = label_file.read_document(json_path)
    data["flags"] = flags
    directory = os.path.dirname(json_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    label_file.write_document(json_path, data)


def get_first_true_flag(flags: Dict[str, bool]) -> str:
    for key, value in flags.items():
        if value:
            return key
    return None


def export_image_to_category(image_path: str, category: str, output_dir: str):
    category_dir = resolve_export_directory(output_dir, category)
    os.makedirs(category_dir, exist_ok=True)

    image_name = os.path.basename(image_path)
    dest_path = resolve_path_within_directory(
        category_dir / image_name, output_dir
    )

    if not os.path.exists(dest_path):
        shutil.copy2(image_path, dest_path)


def get_display_text_for_flags(
    flags: Dict[str, bool], labels: List[str]
) -> str:
    if not flags:
        return ""

    active_labels = []
    for label in labels:
        if flags.get(label, False):
            active_labels.append(label)

    return ", ".join(active_labels) if active_labels else ""


def create_ai_prompt_template(labels: List[str], is_multiclass: bool) -> str:
    task_type = "multi-class" if is_multiclass else "multi-label"
    labels_str = ", ".join([f'"{label}"' for label in labels])

    if is_multiclass:
        instruction = "Set exactly ONE category to 'true' that best matches the image, keep all others as 'false'."
    else:
        instruction = "Set ALL applicable categories to 'true', keep non-applicable ones as 'false'."

    return f"""@image
You are an expert image classifier. Your task is to perform {task_type} classification.

Task Definition: Analyze the given image and classify it based on the provided categories.

Available Categories: [{labels_str}]

Instructions:
1. Carefully examine the image and identify the main subject and their activity
2. Be precise - only select categories that clearly match what you observe

Return your result in strict JSON format:
{{{", ".join([f'"{label}": false' for label in labels])}}}

{instruction}"""
