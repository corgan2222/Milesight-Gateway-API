"""Package metadata; the PyPI page is the README."""

from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as readme:
    LONG_DESCRIPTION = readme.read()

setup(
    name="Milesight-Gateway-API",
    version="1.0.0",
    packages=find_packages(),              # Automatically find your package
    # Milesight's payload decoders (MIT) and their index.
    package_data={"milesight_gateway_api": ["codecs/*"]},
    install_requires=[                     # Dependencies needed for your package
        "aiohttp",
        "pycryptodome",
        "urllib3",
    ],
    extras_require={"decoder": ["dukpy>=0.6.0"]},
    author="Stefan Knaak",
    author_email="stefan.knaak@e-shelter.io",
    description=(
        "Client for Milesight LoRaWAN gateways: REST API, web interface, "
        "firmware, payload codecs and a LoRaWAN connection"
    ),
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    url="https://github.com/corgan2222/Milesight-Gateway-API",
    project_urls={
        "Source": "https://github.com/corgan2222/Milesight-Gateway-API",
        "Issues": "https://github.com/corgan2222/Milesight-Gateway-API/issues",
        "Release notes": "https://github.com/corgan2222/Milesight-Gateway-API/releases",
        "Home Assistant integration": "https://github.com/corgan2222/ha-milesight-gateway",
    },
    keywords=["milesight", "lorawan", "gateway", "ug65", "ug67", "home-assistant"],
    classifiers=[
        # The versions the Test workflow runs, and the ones between.
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Development Status :: 5 - Production/Stable",
        "Framework :: AsyncIO",
        "Intended Audience :: Developers",
        "Topic :: Home Automation",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',               # Minimum Python version requirement
)
