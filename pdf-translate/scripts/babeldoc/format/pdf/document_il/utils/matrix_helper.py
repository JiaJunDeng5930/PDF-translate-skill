"""Matrix helper utilities for CTM decomposition and composition.

This module provides functions to:
- Decompose a PDF CTM into translation, rotation, scale, and shear
- Compose a CTM back from translation, rotation, scale, and shear

All comments and docstrings are in English per project guidelines.
"""

from __future__ import annotations

import math

from babeldoc.format.pdf.document_il.il_version_1 import PdfAffineTransform
from babeldoc.format.pdf.document_il.il_version_1 import PdfMatrix

# Local type aliases to avoid importing from pdfminer
Point = tuple[float, float]
Matrix = tuple[float, float, float, float, float, float]


def decompose_ctm(m: Matrix | PdfMatrix) -> PdfAffineTransform:
    """Decompose a PDF CTM into a PdfAffineTransform.

    The PDF current transformation matrix (CTM) is represented as
    ``(a, b, c, d, e, f)`` corresponding to the affine matrix:
    ``[[a, c, e], [b, d, f], [0, 0, 1]]``.

    This function decomposes it into:
    - translation: (tx, ty)
    - rotation: angle in radians (counter-clockwise)
    - scale: (sx, sy)
    - shear: x-shear factor (dimensionless, equals tan(shear_angle))

    The decomposition is based on a QR-like approach commonly used for 2D
    affine matrices. If the linear part is degenerate, sensible fallbacks are
    applied.

    Args:
        m: CTM as ``(a, b, c, d, e, f)``.

    Returns:
        A ``PdfAffineTransform`` instance with fields populated.
    """
    if isinstance(m, PdfMatrix):
        a = m.a
        b = m.b
        c = m.c
        d = m.d
        e = m.e
        f = m.f
        assert a is not None
        assert b is not None
        assert c is not None
        assert d is not None
        assert e is not None
        assert f is not None
    else:
        (a, b, c, d, e, f) = m

    tx, ty = e, f

    # Linear part
    m00, m01 = a, c
    m10, m11 = b, d

    # Scale X is the length of the first column
    sx = math.hypot(m00, m10)

    eps = 1e-12
    if sx < eps:
        # Degenerate first column. Choose rotation = 0, shear = 0, sx = 0.
        rotation = 0.0
        shear = 0.0
        # Then sy is the length of the second column
        sy = math.hypot(m01, m11)
        # Handle reflection
        det = m00 * m11 - m01 * m10
        if det < 0:
            sy = -sy if sy != 0 else -0.0
        return PdfAffineTransform(
            translation_x=tx,
            translation_y=ty,
            rotation=rotation,
            scale_x=sx,
            scale_y=sy,
            shear=shear,
        )

    # Normalize first column to get rotation axis
    r0x = m00 / sx
    r0y = m10 / sx

    # Shear is the projection of the second column onto the first column
    shear = r0x * m01 + r0y * m11

    # Remove the shear component from the second column
    m01_ortho = m01 - shear * r0x
    m11_ortho = m11 - shear * r0y

    # Scale Y is the length of the orthogonalized second column
    sy = math.hypot(m01_ortho, m11_ortho)

    # Determine reflection by determinant sign
    det = m00 * m11 - m01 * m10
    if det < 0:
        sy = -sy if sy != 0 else -0.0
        shear = -shear
        m01_ortho = -m01_ortho
        m11_ortho = -m11_ortho

    # Rotation is the angle of the first column
    rotation = math.atan2(m10, m00)

    return PdfAffineTransform(
        translation_x=tx,
        translation_y=ty,
        rotation=rotation,
        scale_x=sx,
        scale_y=sy,
        shear=shear,
    )






def create_translation_and_scale_matrix(
    translation_x: float, translation_y: float, scale_factor: float
) -> Matrix:
    """Create a transformation matrix for translation and uniform scaling.

    This creates a CTM that first scales uniformly by scale_factor, then translates
    by (translation_x, translation_y).

    Args:
        translation_x: Translation in X direction
        translation_y: Translation in Y direction
        scale_factor: Uniform scale factor for both X and Y

    Returns:
        The CTM matrix (a, b, c, d, e, f)
    """
    # Matrix for uniform scaling and translation:
    # [scale  0      tx]
    # [0      scale  ty]
    # [0      0      1 ]
    # Which maps to CTM (scale, 0, 0, scale, tx, ty)
    return (scale_factor, 0.0, 0.0, scale_factor, translation_x, translation_y)






def matrix_to_bytes(m: Matrix | PdfMatrix) -> bytes:
    try:
        if isinstance(m, PdfMatrix):
            values = (m.a, m.b, m.c, m.d, m.e, m.f)
        else:
            values = tuple(m)
        if len(values) != 6:
            return b""
        matrix = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return b""
    if not all(math.isfinite(value) for value in matrix):
        return b""
    return (
        f" {matrix[0]:.6f} {matrix[1]:.6f} {matrix[2]:.6f} "
        f"{matrix[3]:.6f} {matrix[4]:.6f} {matrix[5]:.6f} cm "
    ).encode()
