from workers.gdelt_archive_downloader import iter_timestamps, latest_export_timestamp


def test_latest_export_timestamp():
    text = "1 hash http://data.gdeltproject.org/gdeltv2/20260716121500.export.CSV.zip\n"
    assert latest_export_timestamp(text) == "20260716121500"


def test_iter_timestamps_is_inclusive_at_fifteen_minute_intervals():
    assert list(iter_timestamps("20260623011500", "20260623014500")) == [
        "20260623011500",
        "20260623013000",
        "20260623014500",
    ]
