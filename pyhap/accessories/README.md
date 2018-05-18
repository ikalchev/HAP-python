# Adding accessories as HAP-python subpackages

If you have implemented an Accessory for a device for HAP-python
and want to share it with others as a subpackage, you can do so using native namespace
packages. Just do the following:

- Make sure you have the following directory structure:
```
pyhap/
    # NO __init__.py here !!!
    accessories/
        # NO __init__.py here !!!
        bulb/
            __init__.py
            ... the code for the bulb accessory ...
```
- Have this in your `setup.py`:
```python
setup(
    ...
    packages=['pyhap.accessories.bulb'],
    ...
)
```

If you upload your package to pip, other users can use your code as
`pip install HAP-python-bulb` or, alternatively, they can just `git clone` and do
`python3 setup.py install`. Others can then use your code by doing:
```
import pyhap.accessories.bulb
```

See [here](https://packaging.python.org/guides/packaging-namespace-packages/#native-namespace-packages)
for more information.
