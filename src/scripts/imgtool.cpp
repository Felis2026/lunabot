/*
从文件读入二进制形式的图片数据，进行图片操作
参数1: 文件名:str 参数2: 输出文件名:str 参数3: 容差:int
输入文件格式: n:int, h:int, w:int, r00:uint8_t g00:uint8_t b00:uint8_t, a00:uint8_t, ...
*/

#include <iostream>
#include <unordered_map>
#include <vector>
#include <string>
#include <cstdio>
#include <cstdlib>
#include <tuple>
#include <cstring>
#include <cerrno>
#include <climits>
#include <limits>
#include <new>

constexpr int dx[4] = {0, 0, -1, 1};
constexpr int dy[4] = {-1, 1, 0, 0};

union Color {
    struct {
        uint8_t r;
        uint8_t g;
        uint8_t b;
        uint8_t a;
    };
    uint32_t key;
    Color() = default;
    Color(uint8_t r, uint8_t g, uint8_t b, uint8_t a) : r(r), g(g), b(b), a(a) {}
    Color(uint32_t key) : key(key) {}
};

int n, h, w;
Color *img = nullptr;

Color& get_color(int t, int y, int x) {
    return img[t * h * w + y * w + x];
}
int quad(int x) {
    return x * x;
}
int color_diff(const Color& a, const Color& b) {
    return quad(int(a.r) - int(b.r)) + quad(int(a.g) - int(b.g)) + quad(int(a.b) - int(b.b));
}
bool check_pos(int y, int x) {
    return y >= 0 && y < h && x >= 0 && x < w;
}

void floodfill(int t, int sy, int sx, const Color& src, const Color& dst, int tolerance) {
    static std::vector<std::tuple<int, int>> stack;
    stack.clear();
    stack.emplace_back(sy, sx);
    get_color(t, sy, sx) = dst;

    // std::cerr << "[cutout] start floodfill (" << t << ", " << sy << ", " << sx << ")" << std::endl;

    while (!stack.empty()) {
        auto [y, x] = stack.back();
        stack.pop_back();

        // std::cerr << "[cutout] floodfill (" << t << ", " << y << ", " << x << ")" << std::endl;
        
        for (int i = 0; i < 4; ++i) {
            int ny = y + dy[i];
            int nx = x + dx[i];
            if (!check_pos(ny, nx)) continue;
            auto& c = get_color(t, ny, nx);
            if (!c.a) continue;
            if (color_diff(c, src) > tolerance) continue;
            c = dst;
            stack.emplace_back(ny, nx);
        }
    }
}

bool parse_int_arg(const char* value, int min_value, int max_value, int& output) {
    if (!value || !*value) return false;
    errno = 0;
    char* end = nullptr;
    long parsed = std::strtol(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed < min_value || parsed > max_value) {
        return false;
    }
    output = static_cast<int>(parsed);
    return true;
}

bool write_exact(FILE* file, const void* data, size_t item_size, size_t count) {
    return std::fwrite(data, item_size, count, file) == count;
}


int main(int argc, char *argv[]) {
    if (argc < 4) {
        std::cerr << "[imgtool-cpp] usage: <input> <output> <command> [args...]" << std::endl;
        return 2;
    }

    std::string filename = argv[1];
    std::string outname = argv[2];
    std::string command = argv[3];

    if ((command == "cutout" && argc != 5) || (command == "shrink" && argc != 6)) {
        std::cerr << "[imgtool-cpp] invalid argument count for command: " << command << std::endl;
        return 2;
    }
    if (command != "cutout" && command != "shrink") {
        std::cerr << "[imgtool-cpp] unknown command: " << command << std::endl;
        return 2;
    }
    
    // 读取图片数据
    FILE *fp = fopen(filename.c_str(), "rb");
    if (!fp) {
        std::cerr << "[imgtool-cpp] error opening file: " << filename << std::endl;
        return 1;
    }
    if (std::fread(&n, sizeof(int), 1, fp) != 1 ||
        std::fread(&h, sizeof(int), 1, fp) != 1 ||
        std::fread(&w, sizeof(int), 1, fp) != 1) {
        std::cerr << "[imgtool-cpp] incomplete image header" << std::endl;
        fclose(fp);
        return 1;
    }

    constexpr size_t max_input_bytes = 1'000'000'000;
    if (n <= 0 || h <= 0 || w <= 0) {
        std::cerr << "[imgtool-cpp] invalid image dimensions" << std::endl;
        fclose(fp);
        return 1;
    }
    const size_t frame_pixels = static_cast<size_t>(h) * static_cast<size_t>(w);
    if (frame_pixels > max_input_bytes / sizeof(Color) ||
        static_cast<size_t>(n) > max_input_bytes / sizeof(Color) / frame_pixels) {
        std::cerr << "[imgtool-cpp] image size too large" << std::endl;
        fclose(fp);
        return 1;
    }
    const size_t pixel_count = static_cast<size_t>(n) * frame_pixels;
    img = new (std::nothrow) Color[pixel_count];
    if (!img) {
        std::cerr << "[imgtool-cpp] unable to allocate image buffer" << std::endl;
        fclose(fp);
        return 1;
    }
    const size_t read_count = std::fread(img, sizeof(Color), pixel_count, fp);
    fclose(fp);
    if (read_count != pixel_count) {
        std::cerr << "[imgtool-cpp] incomplete image payload" << std::endl;
        delete[] img;
        return 1;
    }

    // 返回的额外数据
    std::string extra_ret{};

    // cutout
    if (command == "cutout") {
        int tolerance = 0;
        if (!parse_int_arg(argv[4], 0, 255, tolerance)) {
            std::cerr << "[imgtool-cpp] invalid cutout tolerance" << std::endl;
            delete[] img;
            return 2;
        }
        tolerance = (tolerance * tolerance) * 3;

        std::cerr << "[imgtool-cpp] start to cutout img (" << n << "x" << h << "x" << w << ")" << std::endl;
        // 以第一帧的边缘像素作为参照，找出最常见的颜色
        std::unordered_map<uint32_t, int> edge_color_count{};
        Color max_color;
        int max_color_count = 0;

        auto update_color = [&](Color c) {
            int cnt = ++edge_color_count[c.key];
            if (cnt > max_color_count) {
                max_color_count = cnt;
                max_color = c;
            }
        };

        for (int y = 0; y < h; ++y) { 
            update_color(get_color(0, y, 0));
            update_color(get_color(0, y, w - 1));
        }
        for (int x = 0; x < w; ++x) {
            update_color(get_color(0, 0, x));
            update_color(get_color(0, h - 1, x));
        }

        std::cerr << "[imgtool-cpp] max color: " << int(max_color.r) << " " << int(max_color.g) << " " << int(max_color.b) << " " << int(max_color.a) << std::endl;

        // 遍历每一帧，从边缘开始抠图
        for (int t = 0; t < n; ++t) {
            for (int y = 0; y < h; ++y) {
                for (int x = 0; x < w; ++x) {
                    if (x == 0 || x == w - 1 || y == 0 || y == h - 1) {
                        auto& c = get_color(t, y, x);
                        if (c.a && color_diff(c, max_color) <= tolerance) 
                            floodfill(t, y, x, max_color, Color(0, 0, 0, 0), tolerance);
                    }
                }
            }
        }

        std::cerr << "[imgtool-cpp] cutout done" << std::endl;
    }
    // shrink
    else if (command == "shrink") {
        int alpha_threshold = 0;
        int edge = 0;
        if (!parse_int_arg(argv[4], 0, 255, alpha_threshold) ||
            !parse_int_arg(argv[5], 0, 100, edge)) {
            std::cerr << "[imgtool-cpp] invalid shrink arguments" << std::endl;
            delete[] img;
            return 2;
        }
        // 计算alpha>threshold的最小包围盒
        int x0 = w;
        int y0 = h;
        int x1 = -1;
        int y1 = -1;
        for (int t = 0; t < n; ++t) {
            for (int y = 0; y < h; ++y) {
                for (int x = 0; x < w; ++x) {
                    auto& c = get_color(t, y, x);
                    if (c.a > alpha_threshold) {
                        if (x < x0) x0 = x;
                        if (y < y0) y0 = y;
                        if (x > x1) x1 = x;
                        if (y > y1) y1 = y;
                    }
                }
            }
        }
        
        if (x1 >= x0 && y1 >= y0) {
            const int nw = x1 - x0 + 1;
            const int nh = y1 - y0 + 1;
            const int out_w = nw + 2 * edge;
            const int out_h = nh + 2 * edge;
            const size_t output_pixels = static_cast<size_t>(n) * static_cast<size_t>(out_h) * static_cast<size_t>(out_w);
            if (out_w <= 0 || out_h <= 0 || output_pixels > max_input_bytes / sizeof(Color)) {
                std::cerr << "[imgtool-cpp] shrink output size too large" << std::endl;
                delete[] img;
                return 1;
            }

            Color* new_img = new (std::nothrow) Color[output_pixels];
            if (!new_img) {
                std::cerr << "[imgtool-cpp] unable to allocate shrink output" << std::endl;
                delete[] img;
                return 1;
            }
            std::memset(new_img, 0, output_pixels * sizeof(Color));

            for (int t = 0; t < n; ++t) {
                for (int y = 0; y < out_h; ++y) {
                    for (int x = 0; x < out_w; ++x) {
                        const int src_x = x0 + x - edge;
                        const int src_y = y0 + y - edge;
                        if (src_x >= 0 && src_x < w && src_y >= 0 && src_y < h) {
                            new_img[t * out_h * out_w + y * out_w + x] = get_color(t, src_y, src_x);
                        }
                    }
                }
            }
            h = out_h;
            w = out_w;
            delete[] img;
            img = new_img;

            // bbox 允许为负数，用于表示向原图边界外扩展出的透明区域。
            extra_ret = "{\"bbox\":[" + std::to_string(x0 - edge) + "," +
                std::to_string(y0 - edge) + "," + std::to_string(out_w) + "," +
                std::to_string(out_h) + "]}";
        } else {
            // 全透明图没有可裁剪区域，保留原始尺寸，避免生成异常的 0x0 图像。
            extra_ret = "{\"bbox\":[0,0," + std::to_string(w) + "," + std::to_string(h) + "]}";
        }
    }

    // 输出处理完毕的图像
    FILE *out_fp = fopen(outname.c_str(), "wb");
    if (!out_fp) {
        std::cerr << "[imgtool-cpp] error opening output file: " << outname << std::endl;
        delete[] img;
        return 1;
    }
    const size_t output_pixel_count = static_cast<size_t>(n) * static_cast<size_t>(h) * static_cast<size_t>(w);
    bool write_ok = write_exact(out_fp, &n, sizeof(int), 1) &&
        write_exact(out_fp, &h, sizeof(int), 1) &&
        write_exact(out_fp, &w, sizeof(int), 1) &&
        write_exact(out_fp, img, sizeof(Color), output_pixel_count);

    // 输出额外数据
    int extra_ret_len = extra_ret.size();
    write_ok = write_ok && write_exact(out_fp, &extra_ret_len, sizeof(int), 1);
    if (extra_ret_len > 0) {
        write_ok = write_ok && write_exact(out_fp, extra_ret.data(), sizeof(char), extra_ret_len);
    }

    fclose(out_fp);
    delete[] img;
    if (!write_ok) {
        std::cerr << "[imgtool-cpp] incomplete output write" << std::endl;
        return 1;
    }
    return 0;
}
