# Third-party notices

LCSC KiCad Bridge includes and adapts existing open-source code. Its product name, plugin identifier, and user documentation do not change the original authorship or licensing of that code.

## Import-LIB-KiCad-Plugin

- Project: <https://github.com/Steffen-W/Import-LIB-KiCad-Plugin>
- Original author/maintainer: Steffen Wittemeier and contributors.
- License: GNU General Public License, version 3; the complete text is retained in `LICENSE.txt`.
- Usage: the repository originated from this project. The EasyEDA importer adapter in `plugins/impart_easyeda.py` is retained; the application entry, browser interaction, project-local installation workflow, packaging, and product documentation have been changed for LCSC KiCad Bridge.

Existing copyright and attribution notices in retained source files remain applicable.

## easyeda2kicad

- Original project: <https://github.com/uPesy/easyeda2kicad.py>
- Original author: uPesy and contributors.
- Bundled source repository: <https://github.com/Steffen-W/easyeda2kicad.py>
- License: GNU Affero General Public License, version 3; the complete text is retained in `plugins/easyeda2kicad/LICENSE`.
- Usage: conversion of EasyEDA symbols, footprints, and available 3D models to KiCad formats. The source is bundled at the Git submodule revision recorded by this repository; building does not fetch a newer revision.

The importer adapter also identifies its derivation from this project's command-line importer. The bundled source retains its existing notices.

## Bootstrap 5.3.3

- Project: <https://getbootstrap.com/> and <https://github.com/twbs/bootstrap>.
- Copyright: 2011–2024 The Bootstrap Authors.
- License: MIT; the complete text is bundled at `plugins/ui/vendor/LICENSE.bootstrap`.
- Bundled stylesheet: `plugins/ui/vendor/bootstrap.min.css`, from the official release at <https://github.com/twbs/bootstrap/blob/v5.3.3/dist/css/bootstrap.min.css>.
- License source: <https://github.com/twbs/bootstrap/blob/v5.3.3/LICENSE>.
- Usage: local application-shell layout, buttons, forms, cards, badges, and progress styling. The stylesheet is included in the installation ZIP; the runtime does not request it from a CDN. Bootstrap JavaScript is not included or required.

The application's own HTML, CSS overrides, JavaScript, and geometric SVG icons are kept separately from the bundled Bootstrap stylesheet.

## Runtime dependencies and data

`kicad-python` and wxPython are runtime dependencies and retain their respective licenses. The plugin runtime installs `kicad-python` according to `plugins/requirements.txt`. On Windows, wxPython and its WebView support are supplied by the selected KiCad Python installation; on other platforms, wxPython is also declared as an installable dependency. This installation ZIP does not contain a Python interpreter or those dependency distributions.

Downloaded component data and models come from external services and are not included in the plugin installation ZIP. Their availability and applicable terms are determined by their providers.

The build includes this notice, the project license, the bundled converter license, and Bootstrap's MIT license with the installed plugin. Redistributing the application must preserve the applicable licenses and source-availability obligations.
