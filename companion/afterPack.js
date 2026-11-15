// electron-builder afterPack hook (it runs before the packaged exe is edited). It fails the build instead of shipping the
// wrong icon.
//
// build.win.signAndEditExecutable: false skips the step that writes the app's icon, name and version into the packaged exe.
// The exe then stays byte-identical to electron.exe: Electron's atom icon on the taskbar, on the Start-menu shortcut and in
// Settings > Installed apps (the installer registers the exe's own first icon as the app's DisplayIcon). It had been switched
// off because electron-builder could not unpack its "winCodeSign" tools without symlink rights. The answer to that is below,
// not turning the edit off.
exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "win32") return;
  const opts = context.packager.platformSpecificBuildOptions || {};
  if (opts.signAndEditExecutable === false) {
    throw new Error(
      "build.win.signAndEditExecutable is false: the packaged exe would keep Electron's icon and name. Remove that setting. " +
      "If the build then stops with \"Cannot create symbolic link\", turn on Windows Developer Mode (or run the first build " +
      "from an Administrator terminal once). electron-builder then caches its tools and later builds need neither."
    );
  }
};
