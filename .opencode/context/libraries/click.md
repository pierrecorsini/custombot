### Basic Group with Command Callback

Source: https://github.com/pallets/click/blob/main/docs/commands.md

Defines a Click group with a callback that executes when a subcommand is invoked. Use this to set up shared state or logging for nested commands.

```python
import click

@click.group()
@click.option('--debug/--no-debug', default=False)
def cli(debug):
    click.echo(f"Debug mode is {'on' if debug else 'off'}")

@cli.command()
def sync():
    click.echo('Syncing')
```

--------------------------------

### Create Command Groups with @click.group()

Source: https://context7.com/pallets/click/llms.txt

Use @click.group() to define a base command that hosts subcommands. The context object (ctx.obj) is used to share state across commands.

```python
import click

@click.group()
@click.option('--debug/--no-debug', default=False)
@click.pass_context
def cli(ctx, debug):
    """A CLI for managing databases."""
    ctx.ensure_object(dict)
    ctx.obj['DEBUG'] = debug
    if debug:
        click.echo('Debug mode is on')

@cli.command()
@click.pass_context
def initdb(ctx):
    """Initialize the database."""
    click.echo(f"Initialized the database (debug={ctx.obj['DEBUG']})")

@cli.command()
@click.option('--force', is_flag=True, help='Force drop without confirmation.')
@click.pass_context
def dropdb(ctx, force):
    """Drop the database."""
    if force or click.confirm('Are you sure you want to drop the database?'):
        click.echo('Dropped the database')
    else:
        click.echo('Aborted!')

if __name__ == '__main__':
    cli()
```

```shell-session
$ python db.py --help
Usage: db.py [OPTIONS] COMMAND [ARGS]...

  A CLI for managing databases.

Options:
  --debug / --no-debug
  --help                Show this message and exit.

Commands:
  dropdb  Drop the database.
  initdb  Initialize the database.

$ python db.py --debug initdb
Debug mode is on
Initialized the database (debug=True)

$ python db.py dropdb --force
Dropped the database
```

--------------------------------

### Python: Command Chaining with click.group(chain=True)

Source: https://context7.com/pallets/click/llms.txt

Use `chain=True` on a click.group to allow multiple subcommands to be invoked in a single call, enabling pipeline-style command composition. The result callback processes the results from each chained command.

```python
import click

@click.group(chain=True)
def cli():
    """Text processing pipeline."""
    pass

@cli.command('upper')
def uppercase():
    """Convert to uppercase."""
    def processor(text):
        return text.upper()
    return processor

@cli.command('lower')
def lowercase():
    """Convert to lowercase."""
    def processor(text):
        return text.lower()
    return processor

@cli.command('strip')
def strip():
    """Remove leading/trailing whitespace."""
    def processor(text):
        return text.strip()
    return processor

@cli.result_callback()
@click.pass_context
def process_pipeline(ctx, processors):
    text = click.prompt('Enter text')
    for processor in processors:
        text = processor(text)
    click.echo(f'Result: {text}')

if __name__ == '__main__':
    cli()

```

--------------------------------

### Shell Session Example

Source: https://github.com/pallets/click/blob/main/docs/commands.md

Illustrates the output of the basic group and command example when run from the shell, showing how options affect the group's callback.

```shell
$ tool.py
Usage: tool.py [OPTIONS] COMMAND [ARGS]...

Options:
  --debug / --no-debug
  --help                Show this message and exit.

Commands:
  sync

$ tool.py --debug sync
Debug mode is on
Syncing
```

--------------------------------

### Test a Click Subcommand Invocation

Source: https://github.com/pallets/click/blob/main/docs/testing.md

Invoke a subcommand ('sync') within a Click group ('cli') using `CliRunner`. Test that both the group's and subcommand's output are captured correctly.

```python
from click.testing import CliRunner
from sync import cli

def test_sync():
  runner = CliRunner()
  result = runner.invoke(cli, ['--debug', 'sync'])
  assert result.exit_code == 0
  assert 'Debug mode is on' in result.output
  assert 'Syncing' in result.output
```