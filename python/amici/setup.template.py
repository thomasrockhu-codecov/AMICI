"""AMICI model package setup"""

import os
import sys
from typing import List
from pathlib import Path
from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext

# Add containing directory to path, as we need some modules from the AMICI
# package already for installation
sys.path.insert(0, os.path.dirname(__file__))

from TPL_MODELNAME.custom_commands import (
    set_compiler_specific_extension_options,
    compile_parallel
)
from TPL_MODELNAME.setuptools import (
    get_blas_config,
    get_hdf5_config,
    add_coverage_flags_if_required,
    add_debug_flags_if_required,
    add_openmp_flags,
)

package_root = Path(__file__).parent / "TPL_MODELNAME"
hdf5_enabled = get_hdf5_config()['found']


class ModelBuildExt(build_ext):
    """Custom build_ext"""

    def build_extension(self, ext):
        # Work-around for compiler-specific build options
        set_compiler_specific_extension_options(
            ext, self.compiler.compiler_type)


        # Monkey-patch compiler instance method for parallel compilation
        #  except for Windows, where this seems to be incompatible with
        #  providing swig files. Not investigated further...
        if sys.platform != 'win32':
            import setuptools._distutils.ccompiler
            self.compiler.compile = compile_parallel.__get__(
                self.compiler, setuptools._distutils.ccompiler.CCompiler)

        print(f"Building model extension in {os.getcwd()}")

        build_ext.build_extension(self, ext)

    def find_swig(self) -> str:
        """Find SWIG executable

        Overrides horribly outdated distutils function."""

        from amici.swig import find_swig
        return find_swig()


def get_model_sources() -> List[str]:
    """Get list of source files for the amici base library"""
    import glob
    model_sources = glob.glob('*.cpp')
    try:
        model_sources.remove('main.cpp')
    except ValueError:
        pass
    return model_sources


def get_amici_libs() -> List[str]:
    """
    Get list of libraries for the amici base library
    """
    return ['amici', 'sundials', 'suitesparse']


def get_extension() -> Extension:
    """Get setuptools extension object for this AMICI model package"""

    cxx_flags = []
    linker_flags = []

    try:
        from amici import compiledWithOpenMP
        # Only build model with OpenMP support if AMICI base packages was built
        #  that way
        # (still necessary during the transition part when amici core objects
        #  might be used from either the model package or the amici base
        #  package)
        with_openmp = compiledWithOpenMP()
    except ModuleNotFoundError:
        # if amici is not installed,
        with_openmp = True
    if with_openmp:
        add_openmp_flags(cxx_flags=cxx_flags, ldflags=linker_flags)

    add_coverage_flags_if_required(cxx_flags, linker_flags)
    add_debug_flags_if_required(cxx_flags, linker_flags)

    h5pkgcfg = get_hdf5_config()

    blaspkgcfg = get_blas_config()
    linker_flags.extend(blaspkgcfg.get('extra_link_args', []))

    libraries = [*get_amici_libs(), *blaspkgcfg['libraries']]
    if hdf5_enabled:
        libraries.extend(['hdf5_hl_cpp', 'hdf5_hl', 'hdf5_cpp', 'hdf5'])

    sources = [os.path.join("swig", "TPL_MODELNAME.i"), *get_model_sources()]

    # compiler and linker flags for libamici
    if 'AMICI_CXXFLAGS' in os.environ:
        cxx_flags.extend(os.environ['AMICI_CXXFLAGS'].split(' '))
    if 'AMICI_LDFLAGS' in os.environ:
        linker_flags.extend(os.environ['AMICI_LDFLAGS'].split(' '))

    ext_include_dirs = [
        os.getcwd(),
        os.path.join(package_root, 'include'),
        os.path.join(package_root, "ThirdParty", "gsl"),
        os.path.join(package_root, "ThirdParty", "sundials", "include"),
        os.path.join(package_root, "ThirdParty", "SuiteSparse", "include"),
        *h5pkgcfg['include_dirs'],
        *blaspkgcfg['include_dirs']
    ]

    ext_library_dirs = [
        *h5pkgcfg['library_dirs'],
        *blaspkgcfg['library_dirs'],
        os.path.join(package_root, 'libs')
    ]

    # Build shared object
    ext = Extension(
        'TPL_MODELNAME._TPL_MODELNAME',
        sources=sources,
        include_dirs=ext_include_dirs,
        libraries=libraries,
        library_dirs=ext_library_dirs,
        swig_opts=[
            '-c++', '-modern', '-outdir', 'TPL_MODELNAME',
            '-I%s' % os.path.join(package_root, 'swig'),
            '-I%s' % os.path.join(package_root, 'include'),
        ],
        extra_compile_args=cxx_flags,
        extra_link_args=linker_flags
    )

    # see `set_compiler_specific_extension_options`
    ext.extra_compile_args_mingw32 = ['-std=c++14']
    ext.extra_compile_args_unix = ['-std=c++14']
    ext.extra_compile_args_msvc = ['/std:c++14']

    return ext


# Change working directory to setup.py location
os.chdir(os.path.dirname(os.path.abspath(__file__)))

MODEL_EXT = get_extension()

CLASSIFIERS = [
    'Development Status :: 3 - Alpha',
    'Intended Audience :: Science/Research',
    'Operating System :: POSIX :: Linux',
    'Operating System :: MacOS :: MacOS X',
    'Programming Language :: Python',
    'Programming Language :: C++',
    'Topic :: Scientific/Engineering :: Bio-Informatics',
]

CMDCLASS = {
    # For parallel compilation and custom swig finder
    'build_ext': ModelBuildExt,
}

# Install
setup(
    name='TPL_MODELNAME',
    cmdclass=CMDCLASS,
    version='TPL_PACKAGE_VERSION',
    description='AMICI-generated module for model TPL_MODELNAME',
    url='https://github.com/AMICI-dev/AMICI',
    author='model-author-todo',
    author_email='model-author-todo',
    # license = 'BSD',
    ext_modules=[MODEL_EXT],
    packages=find_packages(),
    install_requires=[],
    extras_require={'wurlitzer': ['wurlitzer']},
    python_requires='>=3.8',
    package_data={},
    zip_safe=False,
    include_package_data=True,
    classifiers=CLASSIFIERS,
)
