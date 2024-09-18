#!/bin/bash
echo "Building the package..."

# Clean previous builds if they exist
rm -rf dist build Milesight_Gateway_API.egg-info

# Build the package
python setup.py sdist bdist_wheel

# Check if the build was successful
if [ -d "dist" ]; then
    echo "Build successful!"
else
    echo "Build failed."
fi
