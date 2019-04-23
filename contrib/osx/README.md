Building Mac OS binaries
========================

This guide explains how to build Electrum binaries for macOS systems.
We build our binaries on El Capitan (10.11.6) as building it on High Sierra
makes the binaries incompatible with older versions.

This assumes that the Xcode + Xcode Command Line tools (and thus git) are already installed. You can install older (and newer!) versions of Xcode from Apple provided you have a devloper account [from the Apple developer downloads site](https://developer.apple.com/download/more/).


## 1. Make sure to freshen git submodules

    git submodule init
    git submodule update

The above ensures that you pull in the OSX helper app, CalinsQRReader.

## 2. Use the provided script to begin building.

    First, remove .egg from previous build:
        - rm -rf ~/Library/Python/3.6/lib/python/site-packages/Electron_Cash_SLP-3.X.X-py3.6.egg

    If installation problems occur, try also:
        - rm -rf ~/Library/Python/3.6 ~/.pyenv ~/Library/Caches/pip ~/Library/Application Support/pyinstaller

    To Build:
        - ./make_osx
    
    Or, if you wish to sign the app when building, provide an Apple developer identity installed on the system for signing:

        - ./make_osx "Developer ID Application: MY NAME (123456789)"

## 2. Done

You should see Electron-Cash.app and Electron-Cash-macosx-3.x.x.dmg in ../dist/. If you provided an identity for signing, these files can even be distributed to other Macs and they will run there without warnings from GateKeeper.
