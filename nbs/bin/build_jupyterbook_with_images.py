"""
Script for Automating Jupyter Book PDF Generation with Images

This script facilitates the process of including images in the PDF version of a Jupyter Book. It first copies a specified 'images' folder into the Jupyter Book build directory and then triggers the Jupyter Book build process to generate a PDF version.

Assumptions:
- The script is located in a 'bin' directory at the root level of the project.
- The 'images' folder is at the root level of the project.
- The Jupyter notebooks are located in a directory named 'nbs' at the project root.

Usage:
- The script should be executed from its location in the 'bin' directory.
- Python 3.x should be installed and available in the system's PATH.
- Jupyter Book and its dependencies should be installed.

Functions:
- main: The main function that executes the script's operations.

Exceptions:
- FileNotFoundError: If the 'images' source folder does not exist.
- subprocess.CalledProcessError: If there is an error in building the Jupyter Book.

"""

import os
import shutil
import subprocess
import argparse

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Copy images to Jupyter Book build directory and build the book with images.')
    args = parser.parse_args()

    
    # Determine the script's current directory
    script_dir = os.path.dirname(os.path.realpath(__file__))
    # Navigate to the project root (assuming the script is in the 'bin' folder)
    project_root = os.path.abspath(os.path.join(script_dir, '..'))

    # Define source and destination paths
    images_src = os.path.join(project_root, 'images')
    images_dest = os.path.join(project_root, '_build', 'html', 'images')
    
    # Check if the source directory exists
    if not os.path.exists(images_src):
        print("Source 'images' folder not found.")
        return
    
    
    # Inform the user about the operation
    print(f"Images will be copied from \n SOURCE: '{images_src}' \n DESTINATION: '{images_dest}'.")

    # Ask for user confirmation
    confirm = input("Do you want to proceed? (yes/no): ").strip().lower()
    if confirm != 'yes':
        print("Operation cancelled by the user.")
        return

    # Create destination directory if it does not exist
    os.makedirs(images_dest, exist_ok=True)
    
    
    # Copy the 'images' folder
    shutil.copytree(images_src, images_dest, dirs_exist_ok=True)
    print("Images copied successfully.")

    # Build the Jupyter Book
    try:
        subprocess.run(['jupyter-book', 'build', os.path.join(project_root, ''), '--builder', 'pdfhtml'], check=True)
        print("Jupyter Book built successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error in building Jupyter Book: {e}")


if __name__ == "__main__":    
    main()
