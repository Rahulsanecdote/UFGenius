"""Measurement-only research helpers.

Nothing in this package may import the executor, the broker, or anything that
places an order. It exists to answer "is this signal any good" with numbers, and
a module that can also trade is a module that can leak a research idea into the
money path by accident.
"""
