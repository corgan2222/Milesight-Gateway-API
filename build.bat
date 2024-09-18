@echo off
echo Building the package...
rem Clean previous builds if they exist
if exist dist rmdir /S /Q dist
if exist build rmdir /S /Q build
if exist Milesight_Gateway_API.egg-info rmdir /S /Q Milesight_Gateway_API.egg-info

rem Build the package
python setup.py sdist bdist_wheel

rem Check if the build was successful
if exist dist (
    echo Build successful!
) else (
    echo Build failed.
)

pause
