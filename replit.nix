{ pkgs }:
{
  deps = [
    pkgs.python312Full
    pkgs.tesseract
  ];

  shellHook = ''
    if [ -f requirements.txt ]; then
      python -m pip install --quiet -r requirements.txt
    fi
  '';
}
