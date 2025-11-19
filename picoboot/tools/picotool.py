from picoboot import PicoBoot

pb = PicoBoot.open()
data = pb.guess_flash_size()
print(data)

