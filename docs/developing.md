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

### Expected standards

This section will be enhanced following discussions as we go.

However, here's a starting point:

* Variable names should be `lower_case_with_underscores` - this fits
with most existing variables, is a common standard for Python code,
and also allows the spell checking to check individual words within
the variable name.

### Enforced standards

Certain coding standards are enforced within the repository.
Some of them can be auto-fixed, if you do a commit that
fails one of those standards; other issues will need fixing
first, as your pull request won't merge in GitHub until it passes.

These standards are enforced by [pre-commit](https://pre-commit.com),
a tool which is able to run other tools to check, and potentially fix
(for certain types of issues) any mistakes you've made.

The `.pre-commit-config.yaml` file lists all checks that are
currently carried out within the repository. Bear in mind that
these checks are done according to the config within that file
in the branch that you are working in,
so if someone adds a new check, or changes some of the related settings,
it won't apply on your branch until you've merged in their changes.

Some of the tools have their own related config files:

* CSpell - `.cspell.json` and `.cspell/custom-dictionary-workspace.txt`
* Black - `pyproject.toml`
* Markdown Lint - `.markdownlint.jsonc`

Additional notes on some of the standards:

* CSpell - if you have the spelling check failing due to a word which is valid
but is not in the in-built dictionary, please add that word to the end 
of `.cspell/custom-dictionary-workspace.txt` and stage those changes.
The spell-check should then pass. Note, the dictionary file will get
re-sorted alphabetically when you run `pre-commit`, so you'll need to
re-stage the file after it's been sorted.

#### Running the checks locally

If you are using a Codespaces environment, you'll already have `pre-commit`
installed automatically. You can run it manually, or automatically.

Running `pre-commit` manually:

* Running `pre-commit` will run all the checks against any files that you
have modified and staged.

* Alternatively, running `pre-commit run --all-files` will run all the checks
against all files within the repository.

* Note that if `pre-commit` makes any changes to any files when it runs,
those changes will not be staged. You will need to stage those changes too
before committing.

* You may notice `pre-commit` mentioning about stashing changes - this is
because when it runs, any changes that aren't stages are stashed (saved
away temporarily) so it runs against only the staged changes;
after it has run, it pulls back those stashed changes, so they appear
again (still unstaged).

Running `pre-commit` automatically:

* If you run `pre-commit install` it will install a pre-commit hook -
this is a file which tells `git` to run some code each type you do a
particular action (a pre-commit hook runs at the start of processing
a commit, but there are other hooks e.g. pre-push).

* Now, any time you perform a commit, `pre-commit` will run
automatically on the staged files - this is a handy way of making sure
that you don't accidentally commit code which will fail checks later.

* You can still run it manually as outlined above, in addition to the
automated checks that it will do on commits.

#### Running the checks from within GitHub

When commits are done on pull requests, and in any other scenarios
added to the `on` section of`.github/workflows/linting.yml`,
the GitHub Actions in that file will run.

In particular, the [pre-commit.ci lite](https://pre-commit.ci/lite.html)
action will run. This uses the code [here](https://github.com/pre-commit-ci/lite-action)
to run the same checks that get run locally
(as described in the `.pre-commit-config.yaml` file).

This will cause your commit, branch or pull request to get either a green tick
or a red cross against it, to show whether the code passed the checks or not.
This will happen automatically, when you push code on a branch that has a
pull request.

In addition, if `pre-commit` finds any errors that it is able to fix
(e.g. a missing blank line at the end of a file, or trailing whitespace),
it will do a commit of its own to fix those problems, and will push that
commit back to your branch on GitHub. This will then trigger another run,
which should now pass.

**Note**: This means that `pre-commit` will be adding commits to
your branch - this will need you to be pulling changes from GitHub
so you pick up the changes that have been added by `pre-commit`
otherwise you will hit a problem when you next try to push a commit
on your branch. You can pull in those changes by running `git pull`
, which is the equivalent of running `git fetch` then `git merge` .
This is no different to working on the same branch with another developer,
but it will be different to the workflow most of us have when working
on Predbat.
