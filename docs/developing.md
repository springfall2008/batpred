# Developing on Predbat

## Creating a fork

Using GitHub, take a fork of Predbat - effectively, this creates
a copy of the main repository, but in your personal space.
There, you can create branches to develop on.

## Pull requests

Once you've completed your work on your branch, you can create a
pull request (PR) to merge your work back in to the `main` branch
of Predbat.

This PR should describe the work you've done in a way that
makes it easy for someone to review your work, and either
add comments or approve it.

## Editing the code

There are at least a couple of ways of working on the code, outlined here.

### Using GitHub Codespaces

Especially if you don't need to have a running Home Assistant system
to make the changes you're interested in (e.g. for documentation,
quick fixes etc.) a really easy way to work on the code is using
GitHub Codespaces.

This gives you an easy way to spin up an environment with the right
dependencies, and an IDE to work in (Visual Studio Code).

From your fork, click on the Code button, and select the Codespaces tab.
You can create multiple environments, or use a single environment and swap
between branches in it.

Also, you can choose between running the IDE in the browser, or having
your local installation of VS Code connect to the environment that GitHub
Codespaces has created for you. The local installation is better in some
scenarios e.g. if you need to connect to a specific port, such as if you're
working on the documentation.

The Codespaces will be already set up for Python, along with various
Python packages (as defined in `requirements.txt`). The environment
is configured through the config in `.devcontainer/devcontainer.json`.

### Developing locally within Home Assistant

To be documented later.

## Working on the documentation

### The documentation build process

The documentation for the site is built using `mkdocs`, which will
already be installed if you're using a GitHub Codespaces environment.

`mkdocs.yml` contains the config for defining the documentation site,
and is built by `mkdocs` reading the Markdown files in the `docs/` folder,
and creating HTML files from those files.

The building of the documentation is triggered by a GitHub action,
as defined in `.github/workflows/main.yml`.

In short, after configuring the build environment, `mkdocs` builds the
site, then pushes the HTML produced to the `gh-pages` branch,
overwriting whatever was there previously.

GitHub will then detect a new commit on the `gh-pages` branch,
and that will trigger another action to run (as defined by GitHub).
This action will take the HTML files on the `gh-pages` branch,
and will make it available at [https://springfall2008.github.io/batpred/](https://springfall2008.github.io/batpred/).

The documentation will be published as it is, with no further
review process, so please ensure you check the documentation
that will be built before merging it in.

### Working locally on the documentation

If you are making changes to the documentation, you can see
a live updated version of the documentation as it will be
built and deployed.

To do this, run `mkdocs serve` - this will cause `mkdocs` to build the
documentation site, and to temporarily publish it on port 8000 - it will
show the link where you can access the documentation.

Also, it will watch the `docs/` folder, and any time you change the
files, it will rebuild the site, allowing you to see changes to
the Markdown files in your browser within a few seconds.

The site will continue being served until you press `CTRL-C` to
end the `mkdocs serve` command.

*Note, accessing the site published by `mkdocs serve` is not
possible if you are using Codespaces to run VS Code in the browser,
but it is possible if you're using it via VS Code running locally,
due to the way in which ports on your environment are shared.*

## Coding standards

TBC once we agree them
