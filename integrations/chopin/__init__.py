"""Optional Chopin pilot. Loaded only when explicitly installed as a Hermes plugin."""


def register(ctx):
    from .plugin import register as register_plugin
    register_plugin(ctx)
