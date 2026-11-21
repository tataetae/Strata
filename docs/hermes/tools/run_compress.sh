#!/bin/bash
# Fire the Hermes summariser on one session, standalone.
#
# The runtime is the 3.14 store python with the managed install's site-packages on
# sys.path (the shape the tree launcher uses): v0.23.3's pins are python_version >= 3.14,
# and 'regex' (needed by strata_tokenizer) lives only in the 3.14 site-packages.
INSTALL=/home/clara/.hermes/installs/bc715d3ced86a560/environments/7f730d7aa07745b0ad4e3778bfd0e478
SITE=$INSTALL/venv/lib/python3.14/site-packages
REPO=/home/clara/.hermes/hermes-agent
PROFILE_HOME="$1"; SESSION_ID="$2"; FOCUS="${3:-}"
HERMES_DISABLE_LAZY_INSTALLS=1 HERMES_HOME="$PROFILE_HOME" \
exec $INSTALL/venv/bin/python -I -c \
  "import sys; sys.path.insert(0,'$SITE'); sys.path.insert(0,'$REPO'); sys.path.insert(0,'/tmp/opencode'); sys.argv=['fire.py','$PROFILE_HOME','$SESSION_ID','$FOCUS']; import runpy; runpy.run_path('/tmp/opencode/fire_compress.py',run_name='__main__')"
