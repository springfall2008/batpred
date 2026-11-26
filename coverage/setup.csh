python3 -m venv venv
source venv/bin/activate
# --disable-pip-version-check: the venv's pip comes from the Python install, so pip would otherwise print a
# "new release of pip is available" notice on every run. Updating pip isn't needed to install the requirements.
pip install --disable-pip-version-check -r ../requirements.txt
