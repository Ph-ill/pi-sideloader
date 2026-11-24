from sideload_pi.progress import InstallErrorEvent, ProgressEvent, parse_installer_output


def test_cr_updates_and_real_percents_win():
    text = "Install: CreatingStagingDirectory\rInstall: ExtractingPackage (15%)\rInstall: Complete\n"
    events = parse_installer_output(text)
    assert [type(item) for item in events] == [ProgressEvent, ProgressEvent, ProgressEvent]
    assert events[0].stage == "CreatingStagingDirectory"
    assert events[0].percent == 5
    assert events[0].approximate is True
    assert events[1].percent == 15
    assert events[1].approximate is False
    assert events[2].complete is True
    assert events[2].percent == 100


def test_install_error_line():
    text = 'ERROR: Install failed. Got error "ApplicationVerificationFailed" with code 0xe8008015: no profile\n'
    events = parse_installer_output(text)
    assert len(events) == 1
    assert isinstance(events[0], InstallErrorEvent)
    assert events[0].code == "0xe8008015"
    assert "no profile" in events[0].message()
