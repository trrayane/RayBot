{ pkgs }: {
  deps = [
    pkgs.python312Full
    pkgs.python312Packages.pip
    pkgs.zlib
    pkgs.libjpeg_turbo
    pkgs.libpng
    pkgs.freetype
  ];
}
