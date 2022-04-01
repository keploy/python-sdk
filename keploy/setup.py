from setuptools import setup, find_packages

VERSION = '0.0.1' 
DESCRIPTION = 'Keploy'
LONG_DESCRIPTION = 'Keploy Python SDK'

# Setting up
setup(
        name="keploy", 
        version=VERSION,
        author="Keploy Inc.",
        author_email="contact@keploy.io",
        description=DESCRIPTION,
        long_description=LONG_DESCRIPTION,
        packages=find_packages(),
        install_requires=[], # add any additional packages that needs to be installed along with your package.
        
        keywords=['keploy', 'python', 'sdk'],
        classifiers= [
            "Development Status :: 3 - Alpha",
            "Intended Audience :: Education",
            "Programming Language :: Python :: 2",
            "Programming Language :: Python :: 3",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: Microsoft :: Windows",
        ]
)