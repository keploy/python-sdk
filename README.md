[![contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen?logo=github)](CODE_OF_CONDUCT.md)
[![Slack](.github/slack.svg)](https://join.slack.com/t/keploy/shared_invite/zt-12rfbvc01-o54cOG0X1G6eVJTuI_orSA)
[![License](.github/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

**Note** :- Issue Creation is disabled on this Repository, please visit [here](https://github.com/keploy/keploy/issues/new/choose) to submit issue.

# Keploy Python-SDK
This is the client SDK for the [Keploy](https://github.com/keploy/keploy) testing platform. With the Python SDK, you can test both your existing unit test cases in Pytest and create new end-to-end test cases for your applications.
The **HTTP mocks/stubs and tests are the same format** and inter-exchangeable.

## Contents
1. [Installation](#installation)
2. [Usage](#usage)
3. [Community support](#community-support)

## Installation
1. First you need to install [Python(version 3 and above)](https://www.python.org/downloads/)

2. Install the Python-SDK via pip.

```bash
pip install keploy
```

3. Install Keploy from [here](https://github.com/keploy/keploy?tab=readme-ov-file#-quick-installation)

## Usage
Keploy simplifies the testing process by seamlessly generating end-to-end test cases without the need to write unit test files and manage mocks/stubs.

Add a test file with the following code to the directory with all your existing tests. We can call this `test_keploy.py`

```python
from Keploy import run

def test_keploy():
    run("<command-to-run-your-application>")
```
Now to run this testcase along with your another unit testcases, you can run the command below:

```bash
keploy test -c "python3 -m coverage run -m pytest test_keploy.py <other-unit-test-files>" --delay 10 --coverage
```

Hooray🎉! You've sucessfully got the coverage of your Keploy recorded api tests using Pytest.

## Community support
We'd love to collaborate with you to make Keploy.io great. To get started:
* [Slack](https://join.slack.com/t/keploy/shared_invite/zt-12rfbvc01-o54cOG0X1G6eVJTuI_orSA) - Discussions with the community and the team.
* [GitHub](https://github.com/keploy/keploy/issues) - For bug reports and feature requests.