Cut release + homepage workflow

Copy all files into the repository root, replacing older release scripts.

Before releasing:
  git add .
  git commit -m "Prepare Cut release"
  git push

Release the current VERSION.txt and increment the build number:
  chmod +x build_macos_release_v3.sh release_signed.sh         publish_github_release.sh release_and_deploy_homepage.sh         deploy_homepage.sh

  ./release_and_deploy_homepage.sh

Set a new marketing version:
  ./release_and_deploy_homepage.sh --version 0.1.1

Publish a prerelease and point the homepage to it:
  ./release_and_deploy_homepage.sh --prerelease

Test only the homepage step after publishing:
  ./release_and_deploy_homepage.sh --skip-build --homepage-dry-run

The default command commits VERSION.txt and BUILD_NUMBER.txt, pushes the
current branch, publishes a stable GitHub Latest release, and deploys the
homepage with --delete-remote.
