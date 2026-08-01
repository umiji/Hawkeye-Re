"""Developer-only tooling. Not part of the installed `hawkeye` wheel
(see pyproject's `[tool.hatch.build.targets.wheel] packages`), and nothing
in `hawkeye/` may import from here — this package may read the engine, but
the engine must never depend on its debug view.
"""
