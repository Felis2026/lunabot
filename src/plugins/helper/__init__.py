from ..utils import *
from .status_registry import build_help_status_context, get_help_status_prefix
import glob

config = Config('helper')
logger = get_logger('Helper')
file_db = get_file_db('data/helper/db.json', logger)
gbl = get_group_black_list(file_db, logger, 'helper')
cd = ColdDown(file_db, logger)


HELP_DOCS_PATH = "helps/{name}.md"

HELP_BASIC_SERVICES = [
    "general",
    "alive",
]

HELP_GROUP_SERVICES = [
    "sekai",
    "gallery-tag",
    "nekochat",
    "rollpig",
    "msxray",
    "cron",
    "imgtool",
    "imgexp",
    "random",
    "math",
    "record",
    "sta",
    "water",
]

HELP_ADMIN_TOGGLE_SERVICES = {
    "cron",
    "gallery-tag",
    "imgexp",
    "imgtool",
    "math",
    "random",
    "record",
    "rollpig",
}

HELP_SUPERUSER_TOGGLE_SERVICES = {
    "sekai",
    "nekochat",
    "msxray",
    "sta",
    "water",
}

HELP_IMG_SCALE = 0.8
HELP_IMG_WIDTH = 600
HELP_IMG_INTERSECT = 20

help = CmdHandler(['/help', '/帮助'], logger, block=True)
help.check_wblist(gbl).check_cdrate(cd)
@help.handle()
async def _(ctx: HandlerContext):
    args = ctx.get_args().strip()
    status_ctx = build_help_status_context(getattr(ctx, 'group_id', None))

    help_doc_paths = glob.glob(HELP_DOCS_PATH.format(name='*'))
    help_names = []
    help_decs = []
    for path in help_doc_paths:
        try:
            if path.endswith('main.md'): continue
            with open(path, 'r', encoding='utf-8') as f:
                first_line = f.readline().strip()
            help_decs.append(first_line.split()[1])
            help_names.append(Path(path).stem)
        except:
            pass

    if not args or args not in help_names:
        help_desc_map = {
            name: desc
            for name, desc in zip(help_names, help_decs)
        }

        def render_group_service(name: str) -> str:
            desc = help_desc_map[name]
            status_prefix = get_help_status_prefix(name, status_ctx) if status_ctx is not None else ""
            permission_suffix = ""
            if name in HELP_ADMIN_TOGGLE_SERVICES:
                permission_suffix = " 🛠️"
            elif name in HELP_SUPERUSER_TOGGLE_SERVICES:
                permission_suffix = " 🔒"
            prefix = f"{status_prefix} " if status_prefix else ""
            return f"{prefix}{name} - {desc}{permission_suffix}"

        basic_lines = [
            f"{name} - {help_desc_map[name]}"
            for name in HELP_BASIC_SERVICES
            if name in help_desc_map
        ]

        group_lines = [
            render_group_service(name)
            for name in HELP_GROUP_SERVICES
            if name in help_desc_map
        ]

        template: str = config.get('template')
        template = template.format(
            basic_service_list="\n".join(basic_lines).strip(),
            group_service_list="\n".join(group_lines).strip(),
        )
        return await ctx.asend_fold_msg_adaptive(template.strip(), need_reply=False)

    else:
        try:
            # 尝试从缓存读取
            doc_path = HELP_DOCS_PATH.format(name=args)
            doc_mtime = os.path.getmtime(doc_path)
            cache_mtime = file_db.get('help_img_cache_mtime', {})
            cache_path = create_parent_folder(f"data/helper/cache/{args}.png")
            if Path(cache_path).exists() and doc_mtime <= cache_mtime.get(args, 0):
                return await ctx.asend_reply_msg(await get_image_cq(cache_path, low_quality=True))
            else:
                logger.info(f"缓存 {args} 帮助文档不存在或已过期，重新渲染")
                doc_text = Path(doc_path).read_text(encoding='utf-8')
                image = await markdown_to_image(doc_text, width=HELP_IMG_WIDTH)
                image = image.resize((int(image.width * HELP_IMG_SCALE), int(image.height * HELP_IMG_SCALE)))
                # 如果长度过长，截成几段再横向拼接发送
                max_height = HELP_IMG_WIDTH * 3
                if image.height > max_height:
                    n = math.floor(math.sqrt(image.height * image.width) / image.width)
                    height = math.ceil(image.height / n)
                    images = []
                    for i in range(0, image.height, height):
                        bbox = [0, i, image.width, i + height]
                        if bbox[1] > 0:
                            bbox[1] = bbox[1] - HELP_IMG_INTERSECT
                        images.append(image.crop(bbox))
                    image = await run_in_pool(concat_images, images, 'h')
                # 保存缓存
                image.save(cache_path)
                cache_mtime[args] = doc_mtime
                file_db.set(f'help_img_cache_mtime', cache_mtime)
                return await ctx.asend_reply_msg(await get_image_cq(image, low_quality=True))

        except Exception as e:
            logger.print_exc(f"渲染 {doc_path} 帮助文档失败")
            return await ctx.asend_reply_msg(f"帮助文档渲染失败")
            

