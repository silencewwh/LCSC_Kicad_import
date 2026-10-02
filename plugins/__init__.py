"""LCSC KiCad Bridge.

KiCad 10 loads this package through plugin.json and its IPC action. Importing
the package must not register a pcbnew ActionPlugin: that API is PCB-only and
would create a second toolbar button when both plugin loaders are enabled.
"""
