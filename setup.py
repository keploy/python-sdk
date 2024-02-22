from setuptools import setup, find_packages

VERSION = '2.0.0-alpha11'
DESCRIPTION = 'Keploy'
LONG_DESCRIPTION = 'Keploy Python SDK'

# Setting up
setup(
        name="keploy",
        version=VERSION,
        author="Keploy Inc.",
        author_email="hello@keploy.io",
        description="Run your unit tests with Keploy",
        long_description="This module allows you to run your unit tests along with pytest and get coverage for the same using Keploy",
        py_modules=['Keploy'],
        install_requires=[], # add any additional packages that needs to be installed along with your package.

        keywords=['keploy', 'python', 'sdk'],
        classifiers= [
            "Development Status :: 3 - Alpha",
            "Intended Audience :: Education",
            "Programming Language :: Python :: 2",
            "Programming Language :: Python :: 3",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: Microsoft :: Windows",
            "Operating System :: POSIX :: Linux",
        ]
)