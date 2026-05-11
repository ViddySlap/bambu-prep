import click


@click.group()
def main() -> None:
    """bambu-prep: build unsliced .3mf plates for Bambu Studio."""


@main.command()
def version() -> None:
    from bambu_prep import __version__

    click.echo(__version__)
