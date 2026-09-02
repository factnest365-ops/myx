class Myx < Formula
  desc "Terminal music player for YouTube/YT Music radio"
  homepage "https://github.com/factnest365-ops/myx"
  # Update url + sha256 on each release:
  #   url "https://github.com/factnest365-ops/myx/archive/refs/tags/v0.2.0.tar.gz"
  #   sha256 "<sha256 of that tarball>"
  url "https://github.com/factnest365-ops/myx/archive/refs/tags/v0.2.0.tar.gz"
  sha256 "REPLACE_WITH_SHA256_OF_RELEASE_TARBALL"
  license "MIT"
  head "https://github.com/factnest365-ops/myx.git", branch: "main"

  depends_on "python@3.11"
  depends_on "mpv"
  depends_on "yt-dlp"
  depends_on "ffmpeg"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "myx", shell_output("#{bin}/myx --help")
  end

  def caveats
    <<~EOS
      System deps (mpv, yt-dlp, ffmpeg) were installed alongside myx.
      Verify with:  myx doctor
      Auto-fix:     myx doctor --fix
      Launch:       myx  (no args opens the search picker)
    EOS
  end
end
