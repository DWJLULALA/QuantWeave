"""Line-only adapter for the pinned upstream combiner (MIT)."""
class CCombine_Item:
    def __init__(self, item):
        self.time_begin = item.start_idx
        self.time_end = item.end_idx
        self.high = item._high()
        self.low = item._low()
