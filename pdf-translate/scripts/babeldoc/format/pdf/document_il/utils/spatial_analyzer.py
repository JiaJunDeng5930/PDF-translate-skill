"""Spatial relationship analyzer for PDF elements.

This module provides functions to analyze spatial relationships between PDF elements,
particularly for detecting containment relationships between formulas and other elements
like curves and forms.

All comments and docstrings are in English per project guidelines.
"""

from __future__ import annotations

from babeldoc.format.pdf.document_il.il_version_1 import Box
from babeldoc.format.pdf.document_il.utils.layout_helper import calculate_iou_for_boxes


def is_element_contained_in_formula(
    element_box: Box,
    formula_box: Box,
    containment_threshold: float = 0.95,
    tolerance: float = 2.0,
) -> bool:
    """Check if an element is completely contained within a formula with tolerance.

    Args:
        element_box: The bounding box of the element to check
        formula_box: The bounding box of the formula
        containment_threshold: Minimum IoU ratio to consider as contained (default: 0.95)
        tolerance: Tolerance in units to expand formula box for containment check (default: 2.0)

    Returns:
        True if the element is considered contained within the formula
    """
    if element_box is None or formula_box is None:
        return False

    # Expand formula box by tolerance for more lenient containment check
    expanded_formula_box = Box(
        x=formula_box.x - tolerance,
        y=formula_box.y - tolerance,
        x2=formula_box.x2 + tolerance,
        y2=formula_box.y2 + tolerance,
    )

    # Calculate IoU of element box with respect to expanded formula box
    iou = calculate_iou_for_boxes(element_box, expanded_formula_box)
    return iou >= containment_threshold

