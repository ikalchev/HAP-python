:orphan:

==========================================
HAP-python documentation quick start guide
==========================================

This file provides a quick guide on how to compile the HAP-python documentation.


Setup the environment
----------------------

To compile the documentation you need Sphinx and the Read the Docs theme,
with mermaid diagram support. To install them and all their dependencies
run the following command from the repository root::

    pip install -r requirements_docs.txt


Compile the documentation
-------------------------

To compile the documentation (to classic HTML output) run the following
command from this dir::

    make html

Documentation will be generated (in HTML format) inside the ``build/html`` dir.

Read the Docs
-------------

The documentation is built on Read the Docs (the `.readthedocs.yaml` at the
repository root): "latest" builds the ``dev`` branch, tags build the "stable"
versions. The local build above produces the same pages.