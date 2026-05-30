"""Emission category framework.

Each emission category (oil-fuel-fire, structural-damage, etc.) implements
the ``EmissionCategory`` protocol and registers itself via a pyproject
``[project.entry-points."wced.categories"]`` entry. The pipeline discovers
categories at runtime through ``get_registry()``.
"""
