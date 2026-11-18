use image_plugin::rpc::{serve, StdioHost};
use std::io::{stdin, stdout, BufReader};

fn main() {
    let mut host = StdioHost::new(BufReader::new(stdin().lock()), stdout().lock());
    serve(&mut host, "image", image_plugin::run);
}
