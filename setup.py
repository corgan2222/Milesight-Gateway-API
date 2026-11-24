from setuptools import setup, find_packages

setup(
    name="Milesight-Gateway-API",       
    version="0.2.0b1",
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
    description="A python client for interacting with Milesight gateway REST API",  
    long_description=open('README.md').read(),  
    long_description_content_type="text/markdown",  
    url="https://github.com/corgan2222/Milesight-Gateway-API",  
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",  # Use appropriate license
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.9',               # Minimum Python version requirement
)
