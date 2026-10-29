class Myx < Formula
  include Language::Python::Virtualenv

  desc "Terminal music player for YouTube/YT Music radio with DSP, library, and MCP"
  homepage "https://github.com/factnest365-ops/myx"
  url "https://github.com/factnest365-ops/myx/archive/refs/tags/v0.3.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/factnest365-ops/myx.git", branch: "main"

  depends_on "python-setuptools" => :build
  depends_on "ffmpeg"
  depends_on "mpv"
  depends_on "numpy"
  depends_on "pillow"
  depends_on "yt-dlp"

  resource "flit-core" do
    url "https://files.pythonhosted.org/packages/d3/8b/0370578a456986d6995344967a479dd5c9d4e064681550c2a9594bb58b4a/flit_core-4.2.0.tar.gz"
    sha256 "84e4e4d8fdd45ba2613800a3ebc6834de26d47ec9746efc3719f94181137b460"
  end

  resource "packaging" do
    url "https://files.pythonhosted.org/packages/7d/fa/3944b40b07da9ce895c0e6303a5ab7d53da063554f534556b134a54d6093/packaging-26.3.tar.gz"
    sha256 "94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79"
  end

  resource "pathspec" do
    url "https://files.pythonhosted.org/packages/5a/82/42f767fc1c1143d6fd36efb827202a2d997a375e160a71eb2888a925aac1/pathspec-1.1.1.tar.gz"
    sha256 "17db5ecd524104a120e173814c90367a96a98d07c45b2e10c2f3919fff91bf5a"
  end

  resource "pluggy" do
    url "https://files.pythonhosted.org/packages/bf/db/7fc19e6f2dc92a966727031389fc2e08b558f0f25eb7403c1119ad4713cd/pluggy-1.7.0.tar.gz"
    sha256 "d1eaa46ebb595891b860ab086b4d09c8588af65ebd4361b8e8f4bb8920b90ba8"
  end

  resource "trove-classifiers" do
    url "https://files.pythonhosted.org/packages/bf/93/af436dfaa845cab5d96f0adbc1e4f3730532d37fa249e4eb796fb1d7fc82/trove_classifiers-2026.9.21.13.tar.gz"
    sha256 "0a9ebc8d4e2f3e8a22848c5258033035bec17a3012ac3fea16dbaa764489eb71"
  end

  resource "hatchling" do
    url "https://files.pythonhosted.org/packages/7a/f1/e47b937226ae3b093bbd0132eb0b7ee5bfad12c2b5324afd10ae49946b05/hatchling-1.33.0.tar.gz"
    sha256 "4c422bd4d554fcff5de5b73e9596f85e41e9f97f7e553d8f7595ecb31a627bef"
  end

  def python3
    Formula["numpy"].deps
                    .map(&:to_formula)
                    .find { |f| f.name.start_with?("python@") }
                    &.opt_libexec/"bin/python" || "python3"
  end

  def install
    venv = virtualenv_create(libexec, python3, system_site_packages: true)
    %w[flit-core packaging pathspec pluggy trove-classifiers hatchling].each do |res|
      venv.pip_install resource(res)
    end
    venv.pip_install_and_link buildpath
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/myx --version")
    assert_match "usage", shell_output("#{bin}/myx --help").downcase
    assert_match "mpv", shell_output("#{bin}/myx doctor")
  end
end
